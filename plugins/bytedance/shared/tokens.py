"""Token broker with profile/account scoping and single-flight refresh.

Per the design spec §7.2:
- Secrets are resolved under the active Hermes profile and account alias.
- A scoped miss returns a miss — it never borrows an unscoped env value.
- Refresh uses a per-profile/provider/account async single-flight lock.
- Access tokens stay in memory only as long as necessary.
- Refresh tokens and client secrets live in Hermes' secret source, not
  in plugin SQLite.
- Logs expose token fingerprint prefixes only when diagnostics are enabled.
- Revocation invalidates cache and disables the affected account without
  disabling sibling accounts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from plugins.bytedance.shared.errors import ProviderError
from plugins.bytedance.shared.observability import Metrics
from plugins.bytedance.shared.secrets import get_account_secret, get_scoped_secret

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenInfo:
    """Cached token state for one account."""

    access_token: str
    token_type: str
    expires_at: float  # epoch seconds
    scope: str = ""

    @property
    def is_expired(self) -> bool:
        # Refresh 60s before actual expiry for safety margin
        return time.time() >= (self.expires_at - 60)

    @property
    def fingerprint(self) -> str:
        """Truncated token prefix for logging (only when diagnostics enabled)."""
        if len(self.access_token) > 8:
            return self.access_token[:4] + "…"
        return self.access_token[:2] + "…"


@dataclass(frozen=True)
class AccountRef:
    """Canonical account reference.

    ``account_alias`` is a local stable label.  Provider IDs may rotate,
    be scoped to an app, or differ across API products.  They never become
    global identity keys.
    """

    provider: str  # "tiktok_business", "tiktok_creator", "douyin"
    profile: str
    account_alias: str
    provider_account_id: str
    region: Optional[str] = None


class TokenBroker:
    """Resolves and caches provider tokens per (profile, provider, account).

    Tokens are resolved from the active Hermes profile's scoped secret
    store.  A miss under one profile NEVER falls back to another profile's
    value or to a global env var.
    """

    def __init__(self) -> None:
        # Cache: (profile, provider, account_alias) -> TokenInfo
        self._cache: Dict[Tuple[str, str, str], TokenInfo] = {}
        # Single-flight locks: (profile, provider, account_alias) -> Event
        self._refresh_locks: Dict[Tuple[str, str, str], asyncio.Event] = {}
        # Revoked accounts: (profile, provider, account_alias) -> True
        self._revoked: Dict[Tuple[str, str, str], bool] = {}

    def _cache_key(
        self, account: AccountRef
    ) -> Tuple[str, str, str]:
        return (account.profile, account.provider, account.account_alias)

    def get_cached(self, account: AccountRef) -> Optional[TokenInfo]:
        """Return a cached, non-expired token or None."""
        if self._revoked.get(self._cache_key(account), False):
            return None
        info = self._cache.get(self._cache_key(account))
        if info and not info.is_expired:
            return info
        return None

    def set_cached(self, account: AccountRef, info: TokenInfo) -> None:
        """Store a token in cache."""
        self._cache[self._cache_key(account)] = info

    def invalidate(self, account: AccountRef) -> None:
        """Invalidate cached token and mark account as revoked.

        Does NOT disable sibling accounts — only the specified account.
        """
        key = self._cache_key(account)
        self._cache.pop(key, None)
        self._revoked[key] = True
        logger.info(
            "TokenBroker: revoked token for %s:%s",
            account.provider,
            account.account_alias,
        )

    async def acquire(
        self,
        account: AccountRef,
        *,
        access_token_secret: str,
        refresh_token_secret: Optional[str] = None,
        token_url: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        scope: str = "",
    ) -> TokenInfo:
        """Acquire a valid access token for the account.

        If a cached token exists and is not expired, returns it.
        Otherwise, triggers a single-flight refresh.
        """
        # Check cache first
        cached = self.get_cached(account)
        if cached is not None:
            return cached

        # Check revoked
        if self._revoked.get(self._cache_key(account), False):
            raise ProviderError(
                f"Account {account.account_alias} has been revoked",
                retryable=False,
            )

        # Single-flight refresh
        key = self._cache_key(account)
        if key not in self._refresh_locks:
            self._refresh_locks[key] = asyncio.Event()
        lock = self._refresh_locks[key]

        async with lock:
            # Double-check after acquiring lock
            cached = self.get_cached(account)
            if cached is not None:
                return cached

            return await self._refresh_token(
                account,
                access_token_secret=access_token_secret,
                refresh_token_secret=refresh_token_secret,
                token_url=token_url,
                client_id=client_id,
                client_secret=client_secret,
                scope=scope,
            )

    async def _refresh_token(
        self,
        account: AccountRef,
        *,
        access_token_secret: str,
        refresh_token_secret: Optional[str] = None,
        token_url: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        scope: str = "",
    ) -> TokenInfo:
        """Resolve the token from the scoped secret store.

        This base implementation supports static token resolution.
        Subclasses or overrides can implement refresh-token exchange.
        """
        # Resolve access token from account-scoped secret path
        access_token = get_account_secret(
            account.account_alias, access_token_secret
        )
        if not access_token:
            raise ProviderError(
                f"No access token found for account {account.account_alias}",
                retryable=False,
                context={
                    "account_alias": account.account_alias,
                    "provider": account.provider,
                },
            )

        # If we have a refresh token and token_url, do a refresh exchange
        if refresh_token_secret and token_url and client_id and client_secret:
            rt = get_account_secret(account.account_alias, refresh_token_secret)
            if rt:
                try:
                    token_info = await self._do_refresh(
                        token_url,
                        client_id,
                        client_secret,
                        rt,
                        scope=scope,
                    )
                    self.set_cached(account, token_info)
                    Metrics.increment(
                        Metrics or "bytedance_token_refresh_total",
                        labels={"provider": account.provider, "result": "success"},
                    )
                    return token_info
                except ProviderError:
                    Metrics.increment(
                        Metrics or "bytedance_token_refresh_total",
                        labels={"provider": account.provider, "result": "failure"},
                    )
                    raise

        # Static token: no expiry info, so treat as valid until proven otherwise
        info = TokenInfo(
            access_token=access_token,
            token_type="Bearer",
            expires_at=float("inf"),
            scope=scope,
        )
        self.set_cached(account, info)
        return info

    async def _do_refresh(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        *,
        scope: str = "",
    ) -> TokenInfo:
        """Perform an OAuth2 refresh token exchange."""
        from plugins.bytedance.shared.http import BoundedApiClient

        client = BoundedApiClient(token_url, default_endpoint="token_refresh")
        try:
            result = await client.request(
                "POST",
                token_url,
                endpoint="token_refresh",
                json_body={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    **({"scope": scope} if scope else {}),
                },
                idempotency_key=None,  # refresh is POST but per-request
                max_retries=0,  # don't retry refresh tokens
            )

            access_token = result.get("access_token", "")
            token_type = result.get("token_type", "Bearer")
            expires_in = result.get("expires_in", 3600)
            expires_at = time.time() + float(expires_in)
            scope_str = result.get("scope", scope)

            if not access_token:
                raise ProviderError(
                    "Token refresh response missing access_token",
                    retryable=False,
                )

            return TokenInfo(
                access_token=access_token,
                token_type=token_type,
                expires_at=expires_at,
                scope=scope_str,
            )
        finally:
            await client.close()

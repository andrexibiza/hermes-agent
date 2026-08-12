"""Webhook signature validation for the generic webhook adapter."""

import base64
import binascii
import hashlib
import hmac
import logging
import time

try:
    from aiohttp import web
except ImportError:
    web = None  # type: ignore[assignment]


logger = logging.getLogger("gateway.platforms.webhook")


def _hmac_str_equal(provided: str, expected: str) -> bool:
    """Timing-safe equality for two ``str`` values, tolerant of non-ASCII input.

    ``hmac.compare_digest`` raises ``TypeError`` when given a ``str`` that
    contains non-ASCII characters. The ``provided`` value here is an
    attacker-controlled signature/token header on a public, unauthenticated
    webhook endpoint, so a single non-ASCII byte would otherwise raise out of
    the request handler and return a 500 instead of rejecting the request.
    Comparing as UTF-8 bytes keeps the constant-time guarantee while making a
    hostile header fail closed with a clean rejection.
    """
    return hmac.compare_digest(provided.encode(), expected.encode())




class WebhookAuthMixin:
    """Validate webhook signatures while preserving the adapter seam."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._v1_signature_warned: set[str] = set()

    def _validate_signature(
        self, request: "web.Request", body: bytes, secret: str
    ) -> bool:
        """Validate webhook signature (GitHub, GitLab, Svix, generic HMAC-SHA256)."""
        def _header(name: str) -> str:
            return (
                request.headers.get(name, "")
                or request.headers.get(name.lower(), "")
                or request.headers.get(name.upper(), "")
            )

        # Svix / AgentMail:
        #   svix-id: msg_...
        #   svix-timestamp: unix seconds
        #   svix-signature: v1,<base64-hmac> [v1,<base64-hmac> ...]
        # Signed content is: "{id}.{timestamp}.{raw_body}".  Svix secrets
        # usually start with "whsec_" and the remainder is base64-encoded.
        svix_id = _header("svix-id")
        svix_timestamp = _header("svix-timestamp")
        svix_signature = _header("svix-signature")
        if svix_id or svix_timestamp or svix_signature:
            return self._validate_svix_signature(
                body=body,
                secret=secret,
                msg_id=svix_id,
                timestamp=svix_timestamp,
                signature_header=svix_signature,
            )

        # GitHub: X-Hub-Signature-256 = sha256=<hex>
        gh_sig = request.headers.get("X-Hub-Signature-256", "")
        if gh_sig:
            expected = "sha256=" + hmac.new(
                secret.encode(), body, hashlib.sha256
            ).hexdigest()
            return _hmac_str_equal(gh_sig, expected)

        # GitLab: X-Gitlab-Token = <plain secret>
        gl_token = request.headers.get("X-Gitlab-Token", "")
        if gl_token:
            return _hmac_str_equal(gl_token, secret)

        # Generic V2: X-Webhook-Signature-V2 = <hex HMAC-SHA256 of "<timestamp>.<body>">
        #             X-Webhook-Timestamp = <unix seconds> (required for V2)
        # Checked independently of (and before) legacy V1 below — a sender
        # that only ever sends V2 headers must still validate here; nesting
        # this inside `if generic_sig:` would silently skip V2-only senders.
        #
        # The presence of X-Webhook-Signature-V2 alone selects V2 mode and
        # commits to it — it must NOT fall through to the V1 branch just
        # because the timestamp is missing/malformed/expired. A sender
        # migrating to V2 typically sends both V1 and V2 headers together
        # for compatibility; if incomplete V2 fell through to V1, an
        # attacker who captured one such mixed request could strip the
        # X-Webhook-Timestamp header from a replay and have it validate
        # against the still-present, still-unprotected V1 signature instead
        # — silently downgrading a V2-protected request back to the replay
        # hole V2 exists to close.
        v2_sig = request.headers.get("X-Webhook-Signature-V2", "")
        if v2_sig:
            v2_timestamp = request.headers.get("X-Webhook-Timestamp", "")
            if not v2_timestamp:
                logger.warning(
                    "[webhook] Route '%s' sent X-Webhook-Signature-V2 with "
                    "no X-Webhook-Timestamp — rejecting rather than "
                    "falling back to legacy V1",
                    request.match_info.get("route_name", ""),
                )
                return False
            try:
                ts = int(v2_timestamp)
            except (TypeError, ValueError):
                return False
            if abs(int(time.time()) - ts) > 300:
                logger.warning(
                    "[webhook] Route '%s' generic HMAC V2 timestamp outside replay window",
                    request.match_info.get("route_name", ""),
                )
                return False
            signed_content = v2_timestamp.encode() + b"." + body
            expected_v2 = hmac.new(
                secret.encode(), signed_content, hashlib.sha256
            ).hexdigest()
            return _hmac_str_equal(v2_sig, expected_v2)

        # Generic V1 (legacy): X-Webhook-Signature = <hex HMAC-SHA256 of body>
        # (deprecated — no replay protection, since the signature only
        # covers the body: a captured (body, signature) pair replays
        # indefinitely with no timestamp binding it to a specific delivery.)
        # Only reachable when X-Webhook-Signature-V2 was not sent at all —
        # see the guard above.
        generic_sig = request.headers.get("X-Webhook-Signature", "")
        if generic_sig:
            expected = hmac.new(
                secret.encode(), body, hashlib.sha256
            ).hexdigest()
            route_name = request.match_info.get("route_name", "")
            if route_name not in self._v1_signature_warned:
                self._v1_signature_warned.add(route_name)
                logger.warning(
                    "[webhook] Route '%s' uses legacy body-only HMAC (no "
                    "timestamp), which is vulnerable to replay attacks. Add "
                    "an 'X-Webhook-Timestamp' header and switch to "
                    "'X-Webhook-Signature-V2' (HMAC-SHA256 of "
                    "'<timestamp>.<body>').",
                    route_name,
                )
            return _hmac_str_equal(generic_sig, expected)

        # No recognised signature header but secret is configured → reject
        logger.debug(
            "[webhook] Secret configured but no signature header found"
        )
        return False

    def _validate_svix_signature(
        self,
        body: bytes,
        secret: str,
        msg_id: str,
        timestamp: str,
        signature_header: str,
        tolerance_seconds: int = 300,
    ) -> bool:
        """Validate Svix-compatible signatures used by AgentMail webhooks."""
        if not (msg_id and timestamp and signature_header and secret):
            return False

        try:
            ts = int(timestamp)
        except (TypeError, ValueError):
            return False
        if abs(int(time.time()) - ts) > tolerance_seconds:
            logger.warning("[webhook] Svix signature timestamp outside replay window")
            return False

        if secret.startswith("whsec_"):
            encoded_secret = secret.removeprefix("whsec_")
            try:
                key = base64.b64decode(encoded_secret, validate=True)
            except (binascii.Error, ValueError):
                logger.debug("[webhook] Invalid whsec_ Svix signing secret")
                return False
        else:
            # Be permissive for providers that document Svix-style headers but
            # hand out raw shared secrets rather than whsec_ base64 secrets.
            logger.debug("[webhook] Validating Svix-style signature with raw secret")
            key = secret.encode()

        signed_content = msg_id.encode() + b"." + timestamp.encode() + b"." + body
        expected = base64.b64encode(
            hmac.new(key, signed_content, hashlib.sha256).digest()
        ).decode()

        # Svix can send multiple signatures separated by spaces during secret
        # rotation. Each entry is formatted as "vN,<base64>".
        for part in signature_header.split():
            try:
                version, signature = part.split(",", 1)
            except ValueError:
                continue
            if version == "v1" and _hmac_str_equal(signature, expected):
                return True
        return False

"""Freemaxxing model-provider plugin.

Provider discovery imports this module for its ProviderProfile registration side
effect. Runtime construction is deliberately lazy: importing/discovering model
providers must not bind sockets, spawn threads, or resolve credential pools.
"""

import logging
import os
import secrets
import threading

from providers import register_provider
from providers.base import ProviderProfile
from .proxy import Backend, pool, spawn_proxy

logger = logging.getLogger("freemaxxing")

_NOUS_BASE_URL = "https://inference-api.nousresearch.com/v1"
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_HF_BASE_URL = "https://router.huggingface.co/v1"
_DEFAULT_PORT = 11435
_ROUTER_MODEL = "freemaxxing"

# Per-process local proxy credential. This is intentionally not a fixed
# placeholder: arbitrary local processes must not be able to spend the user's
# upstream credentials merely because they can connect to 127.0.0.1.
_LOCAL_TOKEN = secrets.token_urlsafe(32)

_runtime_lock = threading.Lock()
_runtime_pool_built = False


def local_token() -> str:
    """Return this process's local proxy bearer token."""
    return _LOCAL_TOKEN


def _resolve_key(provider_name: str, env_fallbacks: list) -> str:
    """Resolve an upstream API key through the active profile secret scope."""
    try:
        from agent.secret_scope import get_secret, is_multiplex_active

        for env in env_fallbacks:
            val = get_secret(env)
            if val and str(val).strip():
                return str(val).strip()

        if is_multiplex_active():
            return ""
    except Exception:
        try:
            from agent.secret_scope import is_multiplex_active

            if is_multiplex_active():
                return ""
        except Exception:
            pass

    for env in env_fallbacks:
        val = os.environ.get(env, "")
        if val:
            return val
    return ""


def _resolve_nous_credentials():
    """Resolve Nous Portal inference credentials (OAuth JWT), not a static key."""
    try:
        from hermes_cli import auth as auth_mod

        creds = auth_mod.resolve_nous_runtime_credentials()
        api_key = creds.get("api_key", "")
        base_url = creds.get("base_url") or _NOUS_BASE_URL
        return base_url, api_key
    except Exception as exc:
        logger.debug("freemaxxing: nous runtime resolution failed: %s", exc)
        api_key = _resolve_key("nous", ["NOUS_API_KEY"])
        return _NOUS_BASE_URL, api_key


def _add_nous_portal_backend() -> None:
    base_url, api_key = _resolve_nous_credentials()
    pool.add(
        Backend(
            name="nous-portal",
            base_url=base_url,
            api_key=api_key,
            tier=0,
            refresh=_resolve_nous_credentials,
            default_model="deepseek/deepseek-v4-flash-0731",
        )
    )
    if api_key:
        logger.info("freemaxxing: Tier 0 — Nous Portal added (auto-detected)")
    else:
        logger.info(
            "freemaxxing: Tier 0 — Nous Portal added "
            "(JWT deferred to first request)"
        )


def _add_openrouter_backend() -> None:
    api_key = _resolve_key("openrouter", ["OPENROUTER_API_KEY"])
    if not api_key:
        logger.debug("freemaxxing: no OPENROUTER_API_KEY — Tier 1 skipped")
        return
    pool.add(
        Backend(
            name="openrouter",
            base_url=_OPENROUTER_BASE_URL,
            api_key=api_key,
            tier=1,
        )
    )
    logger.info("freemaxxing: Tier 1 — OpenRouter added")


def _add_huggingface_backend() -> None:
    api_key = _resolve_key(
        "huggingface", ["HF_TOKEN", "HUGGING_FACE_TOKEN"]
    )
    if not api_key:
        logger.debug("freemaxxing: no HF_TOKEN — Tier 2 skipped")
        return
    pool.add(
        Backend(
            name="huggingface",
            base_url=_HF_BASE_URL,
            api_key=api_key,
            tier=2,
        )
    )
    logger.info("freemaxxing: Tier 2 — HuggingFace added")


def _build_pool() -> None:
    pool.clear()
    _add_nous_portal_backend()
    _add_openrouter_backend()
    _add_huggingface_backend()


def _configured_port() -> int:
    try:
        return int(os.environ.get("FREEMAXXING_PORT", str(_DEFAULT_PORT)))
    except (TypeError, ValueError):
        return _DEFAULT_PORT


def _loopback_base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/v1"


def ensure_proxy() -> str:
    """Lazily construct the backend pool and start the stable loopback proxy."""
    global _runtime_pool_built

    port = _configured_port()
    with _runtime_lock:
        if not _runtime_pool_built:
            _build_pool()
            _runtime_pool_built = True

        server = spawn_proxy(port=port, token=_LOCAL_TOKEN)

    actual_port = int(server.server_address[1])
    if actual_port != port:
        raise RuntimeError(
            "freemaxxing proxy already bound to unexpected port "
            f"{actual_port}; expected {port}"
        )
    return _loopback_base_url(actual_port)


def _register() -> None:
    port = _configured_port()
    base_url = _loopback_base_url(port)

    # Hermes' generic API-key resolver reads this provider credential from the
    # profile's declared env var. Use a random per-process token rather than the
    # old fixed `local` placeholder. This is registry metadata only; no socket,
    # thread, or upstream pool is created here.
    os.environ["FREEMAXXING_API_KEY"] = _LOCAL_TOKEN

    profile = ProviderProfile(
        name="freemaxxing",
        aliases=("fm", "freemaxxing"),
        display_name="Freemaxxing",
        description="Freemaxxing (Zero-new-config multi-provider failover pool)",
        signup_url="",
        env_vars=("FREEMAXXING_API_KEY",),
        base_url=base_url,
        auth_type="api_key",
        api_mode="chat_completions",
        supports_vision=False,
        supports_noauth_loopback=True,
        default_aux_model="",
        fallback_models=(_ROUTER_MODEL,),
    )
    register_provider(profile)
    logger.info(
        "freemaxxing: provider registered at %s (runtime starts lazily)",
        base_url,
    )


_register()

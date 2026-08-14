"""Freemaxxing model-provider plugin.

Registers a single ``freemaxxing`` provider that fronts a pool of all-free LLM
backends (Nous Portal, OpenRouter, HuggingFace) behind one local OpenAI-
compatible proxy, with model-aware routing and seamless failover.

CRITICAL CONTRACT: model-provider discovery (``providers/__init__.py``
``_import_plugin_dir``) does ``spec.loader.exec_module(module)`` and relies on
the MODULE-LEVEL ``register_provider()`` side effect. It does NOT call a
``register(ctx)`` function — that contract belongs to general plugins. We
therefore self-register at import time, exactly like the bundled
``huggingface`` and ``nous`` profiles do.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from providers import register_provider
from providers.base import ProviderProfile
from proxy import Backend, pool, spawn_proxy

logger = logging.getLogger("freemaxxing")

_NOUS_BASE_URL = "https://inference-api.nousresearch.com/v1"
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_HF_BASE_URL = "https://router.huggingface.co/v1"

# Stable port so the persisted model.base_url survives gateway restarts. A
# dynamic (OS-assigned) port would leave config.yaml pointing at a dead socket
# after every restart, which is what separates a provider from a throwaway
# proxy. Override with FREEMAXXING_PORT if the default collides.
_DEFAULT_PORT = 11435
_ROUTER_MODEL = "freemaxxing"


def _resolve_key(provider_name: str, env_fallbacks: list) -> str:
    """Resolve an API-key env var via the profile-scoped secret scope.

    OpenRouter and HuggingFace are bundled plugin providers, not entries in the
    hand-maintained ``PROVIDER_REGISTRY``, so
    ``resolve_api_key_provider_credentials`` raises ``AuthError`` for them.
    Their keys are plain env-var credentials, read through
    ``agent.secret_scope.get_secret`` — which honors the active profile scope
    under gateway multiplexing and fails closed instead of leaking another
    profile's value (a plain ``os.environ.get`` would leak).
    """
    try:
        from agent.secret_scope import get_secret, is_multiplex_active

        for env in env_fallbacks:
            val = get_secret(env)
            if val and str(val).strip():
                return str(val).strip()

        # Multiplexing is ON: the secret scope is authoritative and we must NOT
        # fall through to the process environment — under a multiplexer
        # ``os.environ`` may hold another profile's value. Fail closed.
        if is_multiplex_active():
            return ""
    except Exception:
        # If the secret scope itself is unavailable (e.g. raised), do not
        # silently reach for os.environ when multiplexing could be active.
        try:
            from agent.secret_scope import is_multiplex_active

            if is_multiplex_active():
                return ""
        except Exception:
            pass

    # Single-profile deployments may provide keys via the process environment
    # rather than a secret scope (systemd Environment=, secret-manager wrappers,
    # plain shell exports). Only reach for os.environ when multiplexing is off.
    for env in env_fallbacks:
        val = os.environ.get(env, "")
        if val:
            return val
    return ""


def _resolve_nous_credentials():
    """Resolve Nous Portal inference credentials (OAuth JWT), not a static key.

    Nous Portal is an OAuth provider, so ``resolve_api_key_provider_credentials``
    raises ``AuthError``. The correct resolver is
    ``resolve_nous_runtime_credentials``, which returns the inference-scoped JWT
    plus the dynamically-resolved inference base URL. Returns
    ``(base_url, api_key)`` or ``(default_base_url, "")``.

    This can run during ``hermes_cli.auth`` import, while that module is only
    partially initialized. In that case the backend is still registered with a
    refresh hook, and the first real request resolves the JWT after auth loading
    has completed.
    """
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
    """Add Nous Portal, resolving its rotating JWT lazily when necessary."""
    base_url, api_key = _resolve_nous_credentials()
    pool.add(
        Backend(
            name="nous-portal",
            base_url=base_url,
            api_key=api_key,
            tier=0,
            refresh=_resolve_nous_credentials,
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
    api_key = _resolve_key("huggingface", ["HF_TOKEN", "HUGGING_FACE_TOKEN"])
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

    if not pool.backends:
        logger.warning(
            "freemaxxing: no backends available. Connect Nous Portal "
            "(hermes auth add nous / hermes setup --portal) or set "
            "OPENROUTER_API_KEY / HF_TOKEN. Provider will 503 until then."
        )


def _is_freemaxxing_selected() -> bool:
    """Return True when the user selected freemaxxing as primary or fallback."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        model_cfg = cfg.get("model") or {}
        if str(model_cfg.get("provider") or "").strip().lower() == "freemaxxing":
            return True

        for entry in cfg.get("fallback_providers") or []:
            if (
                isinstance(entry, dict)
                and str(entry.get("provider") or "").strip().lower()
                == "freemaxxing"
            ):
                return True

        # Legacy single-fallback shape.
        fallback = cfg.get("fallback_model")
        if isinstance(fallback, dict):
            return (
                str(fallback.get("provider") or "").strip().lower()
                == "freemaxxing"
            )
        if isinstance(fallback, list):
            return any(
                isinstance(entry, dict)
                and str(entry.get("provider") or "").strip().lower()
                == "freemaxxing"
                for entry in fallback
            )
    except Exception as exc:
        logger.debug("freemaxxing: selection check failed: %s", exc)
    return False


def _configured_port() -> int:
    try:
        return int(os.environ.get("FREEMAXXING_PORT", str(_DEFAULT_PORT)))
    except (TypeError, ValueError):
        return _DEFAULT_PORT


def _loopback_base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/v1"


def ensure_proxy() -> str:
    """Start the stable loopback proxy on demand and return its base URL.

    This function performs lifecycle setup only. The proxy remains the sole
    authority for backend eligibility, health, concrete-model selection,
    cooldowns, and failover. We deliberately do not fall back to an ephemeral
    port: Hermes may persist this named provider route, and an ephemeral port
    would leave the next process pointing at a dead socket.
    """
    port = _configured_port()
    server = spawn_proxy(port=port)
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

    # Existing primary/fallback configurations must be live at process startup.
    # New selections start the same endpoint from the picker/alias path.
    if _is_freemaxxing_selected():
        try:
            base_url = ensure_proxy()
        except Exception as exc:
            logger.warning(
                "freemaxxing: could not bind stable port %d (%s). "
                "Set FREEMAXXING_PORT to an unused port.",
                port,
                exc,
            )

    profile = ProviderProfile(
        name="freemaxxing",
        aliases=("fm", "freemaxxing"),
        display_name="Freemaxxing",
        description="Freemaxxing (Zero-new-config multi-provider failover pool)",
        signup_url="",
        env_vars=(),
        base_url=base_url,
        auth_type="api_key",
        api_mode="chat_completions",
        supports_vision=False,
        supports_noauth_loopback=True,
        default_aux_model="",
        fallback_models=(_ROUTER_MODEL,),
    )
    # Register BEFORE building the pool. _build_pool() resolves Nous Portal
    # credentials, which triggers `from hermes_cli import auth` (a circular
    # import during discovery). That import runs auth.py's module-level
    # auto-extension of PROVIDER_REGISTRY; if freemaxxing isn't registered yet,
    # the extension silently skips it and `resolve_provider("freemaxxing")`
    # later raises "Unknown provider". Registration first guarantees the
    # profile is visible to that extension no matter the import order.
    register_provider(profile)
    _build_pool()

    logger.info(
        "freemaxxing: provider registered at %s with %d backends (tiers: %s)",
        base_url,
        len(pool.backends),
        sorted({backend.tier for backend in pool.backends}),
    )


# Module-level self-registration — the discovery path imports this module and
# relies on this side effect (see module docstring). Do NOT move this into a
# register(ctx) function.
_register()

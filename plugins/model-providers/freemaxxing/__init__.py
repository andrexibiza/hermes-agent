"""Freemaxxing model-provider plugin.

Registers a single `freemaxxing` provider that fronts a pool of all-free LLM
backends (Nous Portal, OpenRouter, HuggingFace) behind one local OpenAI-
compatible proxy, with model-aware routing and seamless failover.

CRITICAL CONTRACT: model-provider discovery (providers/__init__.py
_import_plugin_dir) does ``spec.loader.exec_module(module)`` and relies on the
MODULE-LEVEL ``register_provider()`` side effect. It does NOT call a
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
from proxy import spawn_proxy, pool, Backend

logger = logging.getLogger("freemaxxing")

_NOUS_BASE_URL = "https://inference-api.nousresearch.com/v1"
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_HF_BASE_URL = "https://router.huggingface.co/v1"

# Stable port so the persisted model.base_url survives gateway restarts. A
# dynamic (OS-assigned) port would leave config.yaml pointing at a dead socket
# after every restart, which is what separates a *provider* from a throwaway
# proxy. Override with FREEMAXXING_PORT if the default collides.
_DEFAULT_PORT = 11435


def _resolve_key(provider_name: str, env_fallbacks: list) -> str:
    """Resolve an API-key env var via the profile-scoped secret scope.

    OpenRouter and HuggingFace are bundled *plugin* providers, not entries in
    the hand-maintained ``PROVIDER_REGISTRY``, so
    ``resolve_api_key_provider_credentials`` raises ``AuthError`` for them.
    Their keys are plain env-var credentials, read through
    ``agent.secret_scope.get_secret`` — which honors the active profile scope
    under gateway multiplexing and fails closed instead of leaking another
    profile's value (a plain ``os.environ.get`` would leak).
    """
    try:
        from agent.secret_scope import get_secret
        for env in env_fallbacks:
            val = get_secret(env)
            if val and str(val).strip():
                return str(val).strip()
    except Exception:
        pass
    # Last-resort fallback: single-profile deployments may provide keys via the
    # process environment rather than a secret scope.
    for env in env_fallbacks:
        val = os.environ.get(env, "")
        if val:
            return val
    return ""


def _resolve_nous_credentials():
    """Resolve Nous Portal inference credentials (OAuth JWT), not a static API key.

    Nous Portal is an OAuth provider, so ``resolve_api_key_provider_credentials``
    raises ``AuthError``. The correct resolver is
    ``resolve_nous_runtime_credentials``, which returns the inference-scoped JWT
    plus the dynamically-resolved inference base URL (may differ from the
    hardcoded production default). Returns (base_url, api_key) or (None, "").

    The import is done lazily inside a retry loop because this function can be
    called during ``providers`` discovery, which itself runs inside
    ``hermes_cli.auth``'s module import — at which point ``auth`` is only
    partially initialized and the runtime resolver does not exist yet. Defer
    the first real resolution to the first request (via the backend's
    ``refresh`` hook) rather than failing at registration.
    """
    try:
        from hermes_cli import auth as auth_mod
        creds = auth_mod.resolve_nous_runtime_credentials()
        api_key = creds.get("api_key", "")
        base_url = creds.get("base_url") or _NOUS_BASE_URL
        return base_url, api_key
    except Exception as e:
        # Could be a genuine "not logged in" OR a circular-import-at-discovery
        # artifact (``auth`` partially initialized). Fall back to env key.
        logger.debug("freemaxxing: nous runtime resolution failed: %s", e)
        api_key = _resolve_key("nous", ["NOUS_API_KEY"])
        return _NOUS_BASE_URL, api_key


def _add_nous_portal_backend() -> None:
    """Add the Nous Portal backend, resolving the JWT lazily if needed.

    At discovery time (during ``hermes_cli.auth``'s own import) the runtime
    resolver is not yet defined, so ``_resolve_nous_credentials`` falls back to
    an empty key. Always add the backend with its ``refresh`` hook, so the
    first real request re-resolves the JWT (auth is fully loaded by then) and
    succeeds without a wasted 401 round-trip.
    """
    base_url, api_key = _resolve_nous_credentials()
    pool.add(Backend(
        name="nous-portal",
        base_url=base_url,
        api_key=api_key,
        tier=0,
        refresh=_resolve_nous_credentials,  # rotating JWT → re-resolve on 401/403 or empty key
    ))
    if api_key:
        logger.info("freemaxxing: Tier 0 — Nous Portal added (auto-detected)")
    else:
        logger.info("freemaxxing: Tier 0 — Nous Portal added (JWT deferred to first request)")


def _add_openrouter_backend() -> None:
    api_key = _resolve_key("openrouter", ["OPENROUTER_API_KEY"])
    if not api_key:
        logger.debug("freemaxxing: no OPENROUTER_API_KEY — Tier 1 skipped")
        return
    pool.add(Backend(name="openrouter", base_url=_OPENROUTER_BASE_URL, api_key=api_key, tier=1))
    logger.info("freemaxxing: Tier 1 — OpenRouter added")


def _add_huggingface_backend() -> None:
    api_key = _resolve_key("huggingface", ["HF_TOKEN", "HUGGING_FACE_TOKEN"])
    if not api_key:
        logger.debug("freemaxxing: no HF_TOKEN — Tier 2 skipped")
        return
    pool.add(Backend(name="huggingface", base_url=_HF_BASE_URL, api_key=api_key, tier=2))
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
    """Return True when the user has actually opted into freemaxxing.

    Spawning the local proxy is a process-wide side effect, so it must only
    happen when the user selected this provider as primary or fallback — not
    on mere provider discovery (which `hermes model` / `hermes doctor` /
    `hermes auth` / setup all trigger). An unselected bundled provider stays
    dormant: profile registered, no listener, zero cost.
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        model_cfg = cfg.get("model") or {}
        if str(model_cfg.get("provider") or "").strip().lower() == "freemaxxing":
            return True
        for entry in cfg.get("fallback_providers") or []:
            if isinstance(entry, dict) and str(entry.get("provider") or "").strip().lower() == "freemaxxing":
                return True
        # Legacy single-fallback shape.
        fb = cfg.get("fallback_model")
        if isinstance(fb, dict) and str(fb.get("provider") or "").strip().lower() == "freemaxxing":
            return True
        if isinstance(fb, list):
            for entry in fb:
                if isinstance(entry, dict) and str(entry.get("provider") or "").strip().lower() == "freemaxxing":
                    return True
    except Exception as e:
        logger.debug("freemaxxing: selection check failed: %s", e)
    return False


def _register() -> None:
    _build_pool()
    try:
        port = int(os.environ.get("FREEMAXXING_PORT", str(_DEFAULT_PORT)))
    except (TypeError, ValueError):
        port = _DEFAULT_PORT

    # Only spawn the proxy when the user selected freemaxxing. Otherwise
    # register the profile dormant — discovery alone must not bind a port.
    actual_port = port
    if _is_freemaxxing_selected():
        # Bind the stable port. If another process already owns it, fall back to
        # an ephemeral port so the provider still registers.
        try:
            server = spawn_proxy(port=port)
            actual_port = server.server_address[1]
        except OSError as e:
            logger.warning(
                "freemaxxing: port %d unavailable (%s); using an ephemeral port", port, e
            )
            server = spawn_proxy(port=0)
            actual_port = server.server_address[1]
    else:
        logger.debug("freemaxxing: not selected — provider registered dormant (no proxy)")
    base_url = f"http://127.0.0.1:{actual_port}/v1"

    profile = ProviderProfile(
        name="freemaxxing",
        aliases=("fm", "freemaxxing"),
        display_name="Freemaxxing",
        description=(
            "Zero-new-config multi-provider failover — Nous Portal, OpenRouter, "
            "HuggingFace. Model-aware pool with seamless failover. Primary or backup."
        ),
        signup_url="",
        env_vars=(),
        base_url=base_url,
        auth_type="api_key",
        api_mode="chat_completions",
        supports_vision=False,
        supports_noauth_loopback=True,
        default_aux_model="",
        fallback_models=(
            "freemaxxing",
        ),
    )
    register_provider(profile)
    logger.info(
        "freemaxxing: provider registered at %s with %d backends (tiers: %s)",
        base_url, len(pool.backends), sorted({b.tier for b in pool.backends}),
    )


# Module-level self-registration — the discovery path imports this module and
# relies on this side effect (see module docstring). Do NOT move this into a
# register(ctx) function.
_register()

# Freemaxxing — Zero-New-Config Multi-Provider Failover for Hermes

Drop it in. Restart Hermes. Done — **if you already have Hermes connected to
Nous Portal.** Freemaxxing routes your agent through a pool of free-tier LLM
backends (Nous Portal, OpenRouter, HuggingFace) with model-aware routing and
seamless failover, so a rate limit on one backend never stops your work.

## The honest promise

There is no universally free, keyless, unlimited LLM API. Freemaxxing's
"zero-new-config" claim means: **it auto-detects the Nous Portal auth you
already have** and uses that as the default tier, with zero new setup. If you
don't have Nous Portal auth, the provider 503s until you connect one backend —
it never silently falls back to a paid provider.

## Backend tiers

| Tier | Source | Key required? | Role |
|------|--------|---------------|------|
| 0 | Nous Portal (OAuth JWT) | Auto-detected from existing Hermes auth | Default — zero new setup |
| 1 | OpenRouter | `OPENROUTER_API_KEY` | Optional — more models, more coverage |
| 2 | HuggingFace | `HF_TOKEN` | Optional — more models, more coverage |

The pool is **model-aware**: a request for a given model only reaches backends
that advertise it. If none advertise it (or the catalog is unknown), it
round-robins across all available backends.

The `freemaxxing` model ID remains opaque inside Hermes core. Hermes never
rewrites it to a concrete vendor/provider; the local proxy is the single
routing authority and substitutes a concrete model only when forwarding.

## Routing and failover

- **Model-aware selection** — prefer backends whose `/models` catalog lists the
  requested model ID.
- **429 rate limit** — honor `Retry-After`, apply cooldown, fail over.
- **5xx / timeout / connection reset** — short cooldown, fail over.
- **Model-not-found (404)** — skip that backend for the request, no cooldown.
- **Auth rejection (401/403)** — skip, no cooldown (do not poison a healthy
  backend over a bad key).
- **Malformed request (other 4xx)** — return 400 immediately, do not retry.
- **Pool exhausted** — return 503 with the last error.

Streaming is passed through transparently (Hermes streams by default).

## Setup

### Primary provider

```bash
hermes config set model.provider freemaxxing
```

### Backup (fallback) provider

```yaml
fallback_providers:
  - provider: freemaxxing
    model: freemaxxing
```

### Optional: expand the pool

```bash
hermes secret set OPENROUTER_API_KEY sk-or-...
hermes secret set HF_TOKEN hf_...
```

Then restart Hermes.

## Verifying

```bash
hermes model                       # freemaxxing should be listed
hermes chat --message "say ok"     # routes through the pool
tail -f ~/.hermes/logs/agent.log | grep freemaxxing
```

Log lines look like:

```
freemaxxing: Tier 0 — Nous Portal added (auto-detected)
freemaxxing: provider registered at http://127.0.0.1:PORT/v1 with 1 backends (tiers: [0])
freemaxxing: model=freemaxxing selected=nous-portal tier=0 attempted=1/3
```

A health view is at `http://127.0.0.1:PORT/healthz` (port in the log line).

## Limitations (v0.1)

- **No vision** — `supports_vision=False`. Use another provider for image tasks.
- **HF is text-only fallback** — tool calls work through Nous Portal and
  OpenRouter; HF's Inference API is lossy for tools.
- **Round-robin among eligible backends** — no latency/quality weighting (v0.2).
- **Three backends only** — no automatic discovery of new free tiers (v0.2).
- **The `freemaxxing` model is a router alias** — it auto-picks the best
  available backend model from the live catalog. You never pin a vendor model
  id; `freemaxxing` means "auto."

## Files

- `plugin.yaml` — manifest (`kind: model-provider`, v1 manifest format)
- `__init__.py` — registers the profile at module level, builds the pool, spawns the proxy
- `proxy.py` — the forward proxy (stdlib-only: `ThreadingHTTPServer` + `urllib`)
- `test_freemaxxing_proxy.py` — 19 unit tests (mock backends, no network)

## Why a local proxy, not a core change

`ProviderProfile` has a fixed `base_url` — it can't route dynamically across
backends. The proxy provides that layer without touching Hermes transport
internals. Hermes sees a normal `chat_completions` provider; the proxy owns
model-affinity, cooldowns, and failover.

# Hermes ByteDance / TikTok Integration

Federated platform plugins for [Hermes Agent](https://hermes-agent.nousresearch.com)
connecting to **TikTok Business Messaging**, **TikTok Organic / Business operations**,
and **Douyin Open Platform** — without erasing their distinct regional, identity,
scope, and policy boundaries.

## What this is

A standalone Python distribution (`hermes-bytedance`) exposing three independently
discoverable Hermes plugin entry points:

| Plugin entry point | Surface | Hermes form |
|---|---|---|
| `tiktok-business` | TikTok Business Messaging API | Gateway platform plugin |
| `douyin` | Douyin Open Platform IM + content APIs | Gateway platform plugin |
| `bytedance-ops` | TikTok Organic / creator publishing / Douyin content ops | Operations tool plugin |

**Shared runtime** (mechanics only — never provider truth):

- Bounded async HTTP client with timeout/retry framework
- Profile/account token broker (no cross-profile fallback)
- Webhook intake with durable composite-key deduplication
- Bounded media broker with SSRF boundary
- Profile-scoped SQLite state store with migrations
- Observability (redacted metrics + logs)

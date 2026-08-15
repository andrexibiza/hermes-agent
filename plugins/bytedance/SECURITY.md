# Security Policy

## Reporting a vulnerability

Security issues in `hermes-bytedance` should be reported following the
Hermes Agent security process. Do **not** open public issues for
vulnerabilities in provider credential handling, webhook verification,
token caching, or media retrieval.

## Design invariants

- No cross-profile secret fallback: each named profile reads only its own config.
- Composite idempotency key `(profile, route, provider, account_alias, event_id)`.
- Provider webhook signature/challenge is verified on raw bytes before any JSON mutation.
- No outbound DM bypasses the provider-specific capability/window policy engine.
- No public content publish occurs without a durable, exact-payload approval record.
- Media retrieval blocks private, loopback, link-local, metadata-service, and
  disallowed IP ranges unless the provider CDN origin is allowlisted.
- Tokens, signatures, raw headers, and message bodies are excluded from logs by default.

# Hermes Webhook Revolution Contract

Source pin: `c59e30f14cec93000af8dc5b5dfd84e113361296`  
Graph SHA-256: `158f3e5ffe5255f3f10f4219aa1caadeee4264c290ce04b1328615fa4fe9fb48`  
Generated: `2026-08-14T20:32:54Z`

## Immutable decisions

1. **Exposure:** unauthenticated `INSECURE_NO_AUTH` routes are loopback-only. Public binds require an explicit authenticated signature mode. Dual-stack default remains supported.
2. **Configuration:** built-in defaults < profile YAML < process environment < active profile secret scope. Every management surface reports provenance without secret values.
3. **Secrets:** route and global secrets persist only by reference. Migration is write → resolve → verify → atomic switch → scrub; pre-switch failure leaves the source byte-identical.
4. **Signatures:** the configured `signature_mode` selects exactly one verifier. No request header may downgrade or select a weaker verifier. Timestamp-bearing modes reject stale/future/malformed timestamps in constant-time comparison paths.
5. **Idempotency:** `(profile, route, provider, delivery_id)` plus body hash. Same-key/same-body is duplicate; same-key/different-body is 409; cross-route fan-out executes once per route. State is bounded by TTL and size.
6. **Execution:** 202 returns an execution identity and status URL. States are accepted → running → completed/failed/cancelled. Cancellation is reported only after the task observes cancellation. Restart reconciliation is explicit.
7. **Interaction:** approvals default to `deny`. `delivery_target` is admitted only when a bidirectional target resolves for the same profile/session; otherwise startup and request handling fail closed.
8. **Callbacks:** HTTP(S) only, redirects refused, private/loopback/link-local/metadata destinations denied by default, every resolved address rechecked, signed versioned envelope, bounded retries, no retry on ordinary 4xx.
9. **Files:** no touched Python/TypeScript/JavaScript file may exceed 2,000 lines. God-file changes occur only through approved seams.
10. **Closure:** no task is complete from an open PR or green CI alone. Exact-head acceptance tests, receipts, graph backlinks, and final cross-surface validation are mandatory.

## Tracker classification

| Class | Items | Disposition |
|---|---|---|
| effective-config | #13240 #24911 #39598 #40324 | absorb |
| secret-persistence | #77471 | adopt |
| intake-idempotency | #7448 #55829 | adopt |
| provider-signatures | #47451 #80327 | adopt |
| profile-session-resume | #57056 #65939 #67277 #71352 #74980 | absorb |
| clarify-approval | #31565 #37284 #71571 #78296 | absorb |
| listener-lifecycle | #4260 #78022 | wontfix-policy/absorb |
| callback-response | #4386 #73828 | adopt |
| delivery-cancel | #20201 #32403 #39999 | absorb |
| model-completion | #43730 #80531 | absorb |
| provider-recipes | #43575 #54693 #66893 #71968 | absorb |
| source-restrictions | #18041 | adopt |

## Graph Gate

- [x] Source pin recorded.
- [x] Semantic graph hashed.
- [x] Contract decisions contain no `needs-decision` entry.
- [ ] Independent witness signs the exact final head after Task 19.
- [ ] Task 20 publishes the terminal release proof.

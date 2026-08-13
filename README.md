# PR #77263 — Usage Dashboard Visual Evidence

Screenshots of the **production** `UsageView` component, captured from the exact
merged PR head after resolving the sidebar/merge conflict with current `origin/main`.

- **PR:** https://github.com/NousResearch/hermes-agent/pull/77263
- **Captured HEAD:** `5b0f5503add946c64f994a34d85c425cf4dc8f5d`
- **Capture method:** vite production build of a throwaway harness that mounts the
  real `UsageView` with the repo's own `usageOverviewFixture` / `usageMeter*Fixture`
  fixtures, served over localhost and screenshotted with headless Chrome (1440× wide,
  390× narrow). Harness files were removed from the working tree before publication;
  the PR tree contains only the production component.
- **Fail-closed behavior visible:** unpriced routes and unknown-cost rows render `—`
  (no fabricated cost); `Market equiv.` and `Captured estimate` render `—` when any
  accounting bucket is absent.

## Files

| File | Deck | Viewport |
|------|------|----------|
| `usage-wide.png`     | Overview | 1440×1700 |
| `usage-narrow.png`   | Overview | 390×2000 |
| `routes-wide.png`    | Routes   | 1440×1700 |
| `routes-narrow.png`  | Routes   | 390×2000 |
| `ledger-wide.png`    | Call ledger | 1440×1700 |
| `ledger-narrow.png`  | Call ledger | 390×2000 |

## Integrity

Run `sha256sum -c SHA256SUMS` (or `certutil -hashfile` on Windows) to verify.

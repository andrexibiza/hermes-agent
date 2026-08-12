# PR #77263 Usage dashboard visual evidence

Exact implementation head: `47bc2b1ba0641aad8a3645ad823a8c6b2a5b161e`

Generated from the real `UsageView`, its real normalization boundary, real styles/providers, and production-shaped raw RPC fixtures. The matrix covers Overview, Routes, and Call ledger at 1440×1000, 1024×768, and 390×844.

The capture validator required:

- all 9 expected PNGs;
- no browser console errors or page errors;
- no document-level horizontal overflow at any viewport;
- the Usage `main` surface to retain `overflow-y: visible` so the surrounding Hermes pane remains the vertical scroll owner.

See `manifest.json` for per-capture probes and `SHA256SUMS` for immutable file hashes.

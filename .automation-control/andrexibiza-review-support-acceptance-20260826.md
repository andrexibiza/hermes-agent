# @andrexibiza review-support acceptance trigger

This branch exists only to emit one guarded `pull_request_target` event against the default-branch automation. It carries no production change and must not be merged.

Expected worker contract:

- load the exact default-branch implementation;
- validate upstream write identity as `andrexibiza`;
- poll the live `NousResearch/hermes-agent` review graph;
- independently verify any proposed response;
- persist deduplication and receipts in merged PR #219.

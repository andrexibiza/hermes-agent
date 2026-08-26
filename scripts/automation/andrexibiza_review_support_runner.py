#!/usr/bin/env python3
"""Bind the review-support worker to a merged PR state carrier.

The fork has repository Issues disabled.  A merged pull request remains a
writable, provenance-bearing GitHub object, so PR #219 carries the worker's
bounded deduplication and receipt ledger without creating a second database or
committing mutable state onto the default branch.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from typing import Any

import andrexibiza_review_support as worker


def _state_pr_number() -> int:
    return max(1, int(os.environ.get("STATE_PR_NUMBER", "219")))


def _load_state(
    self: worker.ReviewWorker,
) -> tuple[dict[str, Any], dict[str, Any]]:
    number = _state_pr_number()
    state_pr, _ = self.state_client.request_json(
        "GET",
        f"/repos/{self.config.state_repo}/pulls/{number}",
        use_cache=False,
    )
    state = worker.parse_state_body(state_pr.get("body") or "")
    if not state:
        state = {
            "version": 1,
            "processed": {},
            "scan_cursor_at": worker.isoformat(
                worker.utcnow()
                - dt.timedelta(minutes=self.config.first_run_lookback_minutes)
            ),
            "created_at": worker.isoformat(worker.utcnow()),
            "state_carrier": (
                f"https://github.com/{self.config.state_repo}/pull/{number}"
            ),
        }
    if not isinstance(state.get("processed"), dict):
        state["processed"] = {}
    return state_pr, state


def _save_state(
    self: worker.ReviewWorker,
    state_pr: dict[str, Any],
    state: dict[str, Any],
) -> None:
    self.state_client.request_json(
        "PATCH",
        f"/repos/{self.config.state_repo}/pulls/{state_pr['number']}",
        {"body": worker.render_state_body(state)},
    )


_original_render_state_body = worker.render_state_body


def _render_state_body(state: dict[str, Any]) -> str:
    return _original_render_state_body(state).replace(
        "This issue is the durable deduplication and receipt ledger for the "
        "scheduled review-support worker.",
        "This merged pull request is the durable deduplication and receipt "
        "carrier for the scheduled review-support worker.",
        1,
    )


worker.ReviewWorker.load_state = _load_state
worker.ReviewWorker.save_state = _save_state
worker.render_state_body = _render_state_body


if __name__ == "__main__":
    sys.exit(worker.main())

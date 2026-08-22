from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(
            f"expected one occurrence in {path}, found {count}: {old!r}"
        )
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "gateway/delivery_ledger.py",
    """    mark_delivered() /    state='delivered'   only on SendResult.success
    mark_failed()         state='failed'      on a definitive rejection
""",
    """    mark_delivered()     state='delivered'   only on complete success
    mark_partial()       state='partial'     on terminal mixed settlement
    mark_failed()        state='failed'      on a definitive rejection
""",
)
replace_once(
    "gateway/delivery_ledger.py",
    """- ``delivered``   — nothing to do; retention prunes.
""",
    """- ``delivered``   — complete success; nothing to do; retention prunes.
- ``partial``     — terminal mixed settlement. Successful child targets made
  observable progress, so aggregate redelivery is forbidden; retention prunes
  after the per-target result has been surfaced to the caller.
""",
)
replace_once(
    "gateway/delivery_ledger.py",
    """def mark_delivered(obligation_id: str) -> None:
    _update_state(obligation_id, "delivered")


def mark_failed(obligation_id: str, error: str = "") -> None:
""",
    """def mark_delivered(obligation_id: str) -> None:
    _update_state(obligation_id, "delivered")


def mark_partial(obligation_id: str, detail: str = "") -> None:
    \"\"\"Record terminal partial progress without aggregate replay authority.

    The unresolved child outcomes remain on ``SendResult.target_results`` for
    selective retry. Replaying the parent obligation would duplicate children
    that already succeeded, so ``partial`` is excluded from restart recovery.
    \"\"\"
    _update_state(obligation_id, "partial", error=detail)


def mark_failed(obligation_id: str, error: str = "") -> None:
""",
)
replace_once(
    "gateway/delivery_ledger.py",
    "WHERE state IN ('delivered', 'abandoned') AND updated_at < ?",
    "WHERE state IN ('delivered', 'partial', 'abandoned') AND updated_at < ?",
)
replace_once(
    "gateway/delivery_ledger.py",
    """ORDER BY CASE state
                                    WHEN 'delivered' THEN 0
                                    WHEN 'abandoned' THEN 1
                                    ELSE 2
                                  END, updated_at ASC
""",
    """ORDER BY CASE state
                                    WHEN 'delivered' THEN 0
                                    WHEN 'partial' THEN 1
                                    WHEN 'abandoned' THEN 2
                                    ELSE 3
                                  END, updated_at ASC
""",
)

replace_once(
    "tests/gateway/test_delivery_ledger.py",
    """class TestObligationId:
""",
    """    def test_partial_is_terminal_and_never_swept(self):
        _record()
        dl.mark_partial("ob-1", "target 1 failed")
        _orphan("ob-1")

        assert _row("ob-1")["state"] == "partial"
        assert dl.sweep_recoverable() == []


class TestObligationId:
""",
)
replace_once(
    "tests/gateway/test_delivery_ledger.py",
    """class TestLedgerEnabled:
""",
    """    def test_old_partial_rows_pruned(self):
        _record()
        dl.mark_partial("ob-1", "one child unresolved")
        with dl._connect() as conn:
            conn.execute(
                "UPDATE delivery_obligations SET updated_at=? WHERE obligation_id=?",
                (time.time() - dl._RETENTION_SECONDS - 60, "ob-1"),
            )
        dl._prune()
        assert _row("ob-1") is None


class TestLedgerEnabled:
""",
)
replace_once(
    "tests/gateway/test_delivery_ledger_producer.py",
    """    @pytest.mark.asyncio
    async def test_slow_ledger_record_does_not_block_event_loop(self):
""",
    """    @pytest.mark.asyncio
    async def test_partial_settlement_is_terminal_not_restart_retryable(self):
        adapter = _Adapter()
        adapter.send = AsyncMock(
            return_value=SendResult(
                success=False,
                partial=True,
                error="target 1 failed after target 0 succeeded",
            )
        )
        await _run(adapter, _event())

        rows = _rows()
        assert len(rows) == 1
        assert rows[0][1] == "partial"
        with dl._connect() as conn:
            conn.execute(
                "UPDATE delivery_obligations SET owner_pid=999999999, "
                "owner_started_at=1"
            )
        assert dl.sweep_recoverable() == []


    @pytest.mark.asyncio
    async def test_slow_ledger_record_does_not_block_event_loop(self):
""",
)

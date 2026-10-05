import argparse
from datetime import datetime, timezone

import pytest

from commuter.config import Settings
from commuter.state import SourceCache
from test_training_sync import Sink, Source, Tokens, connected_store


@pytest.mark.asyncio
async def test_recalculate_without_limit_exports_all_cached_activities(tmp_path):
    from commuter.pipeline import synchronize_training

    store, source, sink = connected_store(tmp_path), Source(), Sink()
    cache = SourceCache(tmp_path / "cache")
    settings = Settings(
        "client", "secret", tmp_path / "commuter.db", "http://localhost"
    )
    try:
        for identifier in range(1, 8):
            detail = await source.get_activity("token", identifier)
            detail["start_date"] = f"2026-09-{20 + identifier}T12:00:00Z"
            cache.save(
                123, identifier,
                {
                    "detail": detail,
                    "streams": {},
                    "reported_zones": {"zones": []},
                },
            )
        source.fail = True
        source.detail_calls.clear()
        result = await synchronize_training(
            settings=settings,
            store=store,
            token_manager=Tokens(),
            strava_client=source,
            sheets=sink,
            cache=cache,
            mode="recalculate",
            now=datetime(2026, 10, 3, tzinfo=timezone.utc),
        )
        assert result.exported == list(range(1, 8))
        assert not result.pending
        assert not result.errors
        assert source.detail_calls == []
    finally:
        store.close()


def test_recalculate_cli_continues_after_time_limited_batch(
    monkeypatch, tmp_path, capsys
):
    import commuter.main as cli
    import commuter.pipeline as pipeline
    import commuter.sheets as sheets

    settings = Settings(
        "client", "secret", tmp_path / "commuter.db", "http://localhost",
        spreadsheet_id="sheet",
    )
    connected_store(tmp_path).close()
    calls = []

    async def synchronize(**kwargs):
        calls.append(kwargs)
        return pipeline.TrainingSyncResult(
            exported=[len(calls)], pending=len(calls) == 1
        )

    class Adapter(Sink):
        def __init__(self, *args, **kwargs):
            super().__init__()

        async def close(self):
            pass

    monkeypatch.setattr(pipeline, "synchronize_training", synchronize)
    monkeypatch.setattr(sheets, "SheetsAdapter", Adapter)
    cli._training_command(
        argparse.Namespace(
            command="training-recalculate", max_activities=None,
            dry_run=False, verbose=False,
        ),
        argparse.ArgumentParser(),
        settings=settings,
    )
    assert len(calls) == 2
    assert calls[1]["run_deadline"] >= calls[0]["run_deadline"]
    assert all(call["mode"] == "recalculate" for call in calls)
    output = capsys.readouterr().out
    assert "pending=True" in output and "pending=False" in output

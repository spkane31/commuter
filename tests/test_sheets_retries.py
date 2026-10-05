from __future__ import annotations

from email.utils import formatdate

import httplib2
import pytest
from googleapiclient.errors import HttpError

from commuter.sheets import SheetsAdapter, SheetsError


class Clock:
    def __init__(self):
        self.elapsed = 100.0
        self.epoch = 1_800_000_000.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds
        self.epoch += seconds


class ThrottledRequest:
    def __init__(self, headers, failures=1):
        self.headers = headers
        self.failures = failures
        self.calls = 0

    def execute(self, *, num_retries):
        assert num_retries == 0
        self.calls += 1
        if self.calls <= self.failures:
            raise HttpError(
                httplib2.Response({"status": "429", **self.headers}),
                b'{"error": {"message": "Quota exceeded"}}',
            )
        return {"updatedRows": 1}


@pytest.fixture
def retry_clock(monkeypatch):
    import commuter.sheets as sheets

    clock = Clock()
    monkeypatch.setattr(sheets.time, "sleep", clock.sleep)
    monkeypatch.setattr(sheets.time, "monotonic", lambda: clock.elapsed)
    monkeypatch.setattr(sheets.time, "time", lambda: clock.epoch)
    monkeypatch.setattr(sheets.random, "random", lambda: 0.25)
    return clock


@pytest.mark.parametrize("header_kind", ["seconds", "http-date"])
def test_sheets_retries_after_server_delay(retry_clock, header_kind):
    header = (
        "30"
        if header_kind == "seconds"
        else formatdate(retry_clock.epoch + 30, usegmt=True)
    )
    adapter = SheetsAdapter("workbook")
    request = ThrottledRequest({"Retry-After": header})

    assert adapter._execute(request) == {"updatedRows": 1}
    assert request.calls == 2
    assert retry_clock.sleeps == [30.0]


@pytest.mark.parametrize("header", [None, "invalid", "-10", "NaN"])
def test_sheets_backoff_can_wait_for_quota_refill(retry_clock, header):
    adapter = SheetsAdapter("workbook")
    request = ThrottledRequest(
        {} if header is None else {"retry-after": header}, failures=6
    )

    assert adapter._execute(request) == {"updatedRows": 1}
    assert request.calls == 7
    assert retry_clock.sleeps == [1.25, 2.25, 4.25, 8.25, 16.25, 32.25]


def test_sheets_defers_all_requests_when_server_delay_exceeds_budget(retry_clock):
    adapter = SheetsAdapter("workbook")
    adapter.deadline = retry_clock.elapsed + 90
    request = ThrottledRequest({"retry-after": "120"})

    with pytest.raises(SheetsError, match="deferred"):
        adapter._execute(request)
    following = ThrottledRequest({}, failures=0)
    with pytest.raises(SheetsError, match="deferred"):
        adapter._execute(following)
    assert request.calls == 1
    assert following.calls == 0
    assert retry_clock.sleeps == []

    # A later run can safely repeat the pending export after the cooldown.
    retry_clock.elapsed += 120
    adapter.deadline = retry_clock.elapsed + 90
    assert adapter._execute(request) == {"updatedRows": 1}
    assert request.calls == 2


def test_sheets_retries_are_bounded_and_cooldown_survives_exhaustion(retry_clock):
    adapter = SheetsAdapter("workbook")
    request = ThrottledRequest({"retry-after": "2"}, failures=100)

    with pytest.raises(SheetsError, match="HTTP 429.*pending"):
        adapter._execute(request)
    assert request.calls == 8
    assert retry_clock.sleeps == [2.0, 2.25, 4.25, 8.25, 16.25, 32.25, 64.0]

    following = ThrottledRequest({}, failures=0)
    assert adapter._execute(following) == {"updatedRows": 1}
    assert retry_clock.sleeps[-1] == 64.0

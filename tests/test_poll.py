import pytest

from internetnl_cli.errors import ApiError, PollTimeout, RunFailed
from internetnl_cli.poll import poll_until_done


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleep_calls.append(seconds)
        self.now += seconds

    sleep_calls: list


class _StatusClient:
    def __init__(self, statuses, host="batch.example"):
        self._statuses = list(statuses)
        self.calls = 0
        self.endpoint_host = host

    def status(self, request_id):
        self.calls += 1
        status = self._statuses[min(self.calls, len(self._statuses)) - 1]
        return {"request": {"request_id": request_id, "status": status}}


def _clock():
    clock = _FakeClock()
    clock.sleep_calls = []
    return clock


def test_unfinished_then_finished_sequence():
    clock = _clock()
    client = _StatusClient(["running", "running", "done"])
    reply = poll_until_done(
        client, "abc", interval=30, max_seconds=3600, sleep=clock.sleep, monotonic=clock.monotonic
    )
    assert reply["request"]["status"] == "done"
    assert clock.sleep_calls == [30, 30]
    assert client.calls == 3


def test_error_status_raises_run_failed():
    clock = _clock()
    client = _StatusClient(["error"])
    with pytest.raises(RunFailed) as excinfo:
        poll_until_done(
            client, "abc", interval=30, max_seconds=3600, sleep=clock.sleep, monotonic=clock.monotonic
        )
    assert excinfo.value.exit_code == 2


def test_cancelled_status_raises_run_failed():
    clock = _clock()
    client = _StatusClient(["cancelled"])
    with pytest.raises(RunFailed):
        poll_until_done(
            client, "abc", interval=30, max_seconds=3600, sleep=clock.sleep, monotonic=clock.monotonic
        )


def test_exceeding_max_seconds_raises_poll_timeout():
    clock = _clock()
    client = _StatusClient(["running"])

    def fake_sleep(seconds):
        clock.sleep_calls.append(seconds)
        clock.now += seconds

    with pytest.raises(PollTimeout) as excinfo:
        poll_until_done(
            client, "abc", interval=30, max_seconds=45, sleep=fake_sleep, monotonic=clock.monotonic
        )
    assert excinfo.value.exit_code == 4


def test_max_seconds_zero_polls_exactly_once_then_times_out():
    clock = _clock()
    client = _StatusClient(["running"])
    with pytest.raises(PollTimeout):
        poll_until_done(
            client, "abc", interval=30, max_seconds=0, sleep=clock.sleep, monotonic=clock.monotonic
        )
    assert client.calls == 1
    assert clock.sleep_calls == []


def test_unknown_status_raises_api_error():
    clock = _clock()
    client = _StatusClient(["something-weird"])
    with pytest.raises(ApiError) as excinfo:
        poll_until_done(
            client, "abc", interval=30, max_seconds=3600, sleep=clock.sleep, monotonic=clock.monotonic
        )
    assert "batch.example" in str(excinfo.value)

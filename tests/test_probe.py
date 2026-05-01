import asyncio

import pytest

from app import probe


class HangingProcess:
    def __init__(self):
        self.returncode = None
        self.killed = False
        self.waited = False

    async def communicate(self):
        await asyncio.sleep(3600)

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        self.waited = True
        return self.returncode


@pytest.mark.parametrize("timeout_seconds", [0.01])
def test_ffprobe_timeout_kills_child_process(monkeypatch, timeout_seconds):
    process = HangingProcess()

    async def fake_create_subprocess_exec(*args, **kwargs):  # noqa: ARG001
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    valid, timed_out, error = asyncio.run(
        probe._run_ffprobe("http://example.invalid/channel", timeout_seconds)
    )

    assert valid is False
    assert timed_out is True
    assert "timeout" in error
    assert process.killed is True
    assert process.waited is True


def test_probe_channel_logs_retry_and_success(monkeypatch, caplog):
    attempts = iter(
        [
            (False, False, "temporary failure"),
            (True, False, None),
        ]
    )

    async def fake_run_ffprobe(channel, timeout_seconds):  # noqa: ARG001
        return next(attempts)

    monkeypatch.setattr(probe, "_run_ffprobe", fake_run_ffprobe)

    settings = probe.ProbeSettings(timeout_seconds=1.0, concurrency=1, retries=1, retry_delay_seconds=0.0)
    target = probe.ProbeTarget(
        url="http://example.invalid/channel",
        name="Channel One",
        fingerprint="abc1234567",
    )

    with caplog.at_level("DEBUG", logger="app.probe"):
        result = asyncio.run(probe.probe_channel(target, settings))

    assert result.valid is True
    assert result.retries_used == 1
    assert "probe_attempt_start" in caplog.text
    assert "probe_retry_scheduled" in caplog.text
    assert "probe_attempt_success" in caplog.text


class CompletedProcess:
    def __init__(self, *, stdout: bytes, stderr: bytes, returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr


def test_ffprobe_rejects_decoder_corruption_on_stderr(monkeypatch):
    payload = b'{"streams":[{"codec_type":"video"}]}'
    stderr = (
        b"[h264 @ 0x123] mmco: unref short failure\n"
        b"[h264 @ 0x123] number of reference frames (0+5) exceeds max (4; probably corrupt input), discarding one\n"
    )

    async def fake_create_subprocess_exec(*args, **kwargs):  # noqa: ARG001
        return CompletedProcess(stdout=payload, stderr=stderr)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    valid, timed_out, error = asyncio.run(
        probe._run_ffprobe("http://example.invalid/channel", 1.0)
    )

    assert valid is False
    assert timed_out is False
    assert "decoder corruption" in error
    assert "mmco" in error


def test_ffprobe_accepts_clean_stream_payload(monkeypatch):
    payload = b'{"streams":[{"codec_type":"video"},{"codec_type":"audio"}]}'

    async def fake_create_subprocess_exec(*args, **kwargs):  # noqa: ARG001
        return CompletedProcess(stdout=payload, stderr=b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    valid, timed_out, error = asyncio.run(
        probe._run_ffprobe("http://example.invalid/channel", 1.0)
    )

    assert valid is True
    assert timed_out is False
    assert error is None

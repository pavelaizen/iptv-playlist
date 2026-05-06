from __future__ import annotations

import subprocess
from types import SimpleNamespace

from app.stream_stability import StabilitySettings, run_stream_stability_test


def test_ffmpeg_stability_test_reports_good_realtime_decode(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stderr="frame= 900 fps=10 speed=1.01x\n",
        )

    monkeypatch.setattr("app.stream_stability.subprocess.run", fake_run)

    result = run_stream_stability_test(
        "http://example.invalid/channel",
        StabilitySettings(duration_seconds=90, timeout_padding_seconds=40),
    )

    assert result.status == "GOOD"
    assert result.frames == 900
    assert result.speed == "1.01"
    assert result.issues == ""


def test_decode_errors_trigger_bad(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stderr="error while decoding MB 12 10\nframe= 400 fps=12 speed=0.42x\n",
        )

    monkeypatch.setattr("app.stream_stability.subprocess.run", fake_run)

    result = run_stream_stability_test(
        "http://example.invalid/channel",
        StabilitySettings(duration_seconds=60),
    )

    assert result.status == "BAD"
    assert "error while decoding" in result.issues
    assert "slow decode speed=0.42x" in result.issues


def test_ffmpeg_stability_test_reports_bad_on_timeout(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=100)

    monkeypatch.setattr("app.stream_stability.subprocess.run", fake_run)

    result = run_stream_stability_test(
        "http://example.invalid/channel",
        StabilitySettings(duration_seconds=60, timeout_padding_seconds=40, retries=0),
    )

    assert result.status == "BAD"
    assert result.frames == 0
    assert result.speed == ""
    assert result.issues == "ffmpeg timeout"


def test_retries_on_connection_timeout(monkeypatch) -> None:
    call_count = 0

    def fake_run(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=100)
        return SimpleNamespace(
            returncode=0,
            stderr="frame= 500 fps=25 speed=1.01x\n",
        )

    monkeypatch.setattr("app.stream_stability.subprocess.run", fake_run)

    result = run_stream_stability_test(
        "http://example.invalid/channel",
        StabilitySettings(duration_seconds=60, retries=1, retry_delay_seconds=0),
    )

    assert call_count == 2
    assert result.status == "GOOD"
    assert result.frames == 500


def test_retries_on_connection_timeout_stderr(monkeypatch) -> None:
    call_count = 0

    def fake_run(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return SimpleNamespace(
                returncode=1,
                stderr="[tcp @ ...] Connection timed out\n",
            )
        return SimpleNamespace(
            returncode=0,
            stderr="frame= 500 fps=25 speed=1.01x\n",
        )

    monkeypatch.setattr("app.stream_stability.subprocess.run", fake_run)

    result = run_stream_stability_test(
        "http://example.invalid/channel",
        StabilitySettings(duration_seconds=60, retries=1, retry_delay_seconds=0),
    )

    assert call_count == 2
    assert result.status == "GOOD"
    assert result.frames == 500


def test_does_not_retry_on_permanent_errors(monkeypatch) -> None:
    call_count = 0

    def fake_run(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        return SimpleNamespace(
            returncode=1,
            stderr="[http @ ...] 404 Not Found\n",
        )

    monkeypatch.setattr("app.stream_stability.subprocess.run", fake_run)

    result = run_stream_stability_test(
        "http://example.invalid/channel",
        StabilitySettings(duration_seconds=60, retries=1),
    )

    assert call_count == 1
    assert result.status == "BAD"
    assert "404 not found" in result.issues


def test_does_not_retry_on_oserror(monkeypatch) -> None:
    call_count = 0

    def fake_run(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        raise OSError("execv failed")

    monkeypatch.setattr("app.stream_stability.subprocess.run", fake_run)

    result = run_stream_stability_test(
        "http://example.invalid/channel",
        StabilitySettings(duration_seconds=60, retries=1),
    )

    assert call_count == 1
    assert result.status == "BAD"


def test_h264_corrupt_input_does_not_trigger_warn(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stderr=(
                "frame= 250 speed=20.4x\n"
                "[h264 @ ...] number of reference frames (0+5) exceeds max "
                "(4; probably corrupt input), discarding one\n"
            ),
        )

    monkeypatch.setattr("app.stream_stability.subprocess.run", fake_run)

    result = run_stream_stability_test(
        "http://example.invalid/channel",
        StabilitySettings(duration_seconds=10),
    )

    assert result.status == "GOOD"
    assert "corrupt" not in result.issues


def test_bad_on_failed_to_open_without_retries(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stderr="[http @ ...] Failed to open\n",
        )

    monkeypatch.setattr("app.stream_stability.subprocess.run", fake_run)

    result = run_stream_stability_test(
        "http://example.invalid/channel",
        StabilitySettings(duration_seconds=60, retries=0),
    )

    assert result.status == "BAD"
    assert "failed to open" in result.issues


_FFMPEG_HEVC_4K_BANNER = (
    "Input #0, hls, from 'http://example.invalid/channel':\n"
    "  Stream #0:0(???): Video: hevc (Main), yuv420p(tv, bt2020nc/bt2020/arib-std-b67), 3840x2160 [SAR 1:1 DAR 16:9], 23.98 fps\n"
    "  Stream #0:1(???): Audio: aac (LC), 48000 Hz, stereo, fltp\n"
)


_FFMPEG_H264_1080P_BANNER = (
    "Input #0, hls, from 'http://example.invalid/channel':\n"
    "  Stream #0:0: Video: h264 (High), yuv420p(tv, bt709), 1920x1080 [SAR 1:1 DAR 16:9], 50 fps\n"
    "  Stream #0:1(heb): Audio: aac (LC), 48000 Hz, stereo, fltp\n"
)


def test_hevc_4k_triggers_warn(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stderr=_FFMPEG_HEVC_4K_BANNER + "frame= 720 fps=24 speed=0.99x\n",
        )

    monkeypatch.setattr("app.stream_stability.subprocess.run", fake_run)

    result = run_stream_stability_test(
        "http://example.invalid/channel",
        StabilitySettings(duration_seconds=30, retries=0),
    )

    assert result.status == "WARN"
    assert "4K HEVC stream, heavy client decode load" in result.issues


def test_hevc_4k_with_warn_patterns_stays_warn(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stderr=(
                _FFMPEG_HEVC_4K_BANNER
                + "frame= 700 fps=23 speed=0.96x\n"
                + "[null @ ...] Application provided invalid, non monotonically increasing dts to muxer\n"
            ),
        )

    monkeypatch.setattr("app.stream_stability.subprocess.run", fake_run)

    result = run_stream_stability_test(
        "http://example.invalid/channel",
        StabilitySettings(duration_seconds=30, retries=0),
    )

    assert result.status == "WARN"
    assert "4K HEVC stream, heavy client decode load" in result.issues
    assert "non monotonically" in result.issues


def test_h264_1080p_with_multiple_eac3_annotates_only(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stderr=(
                "Input #0, hls, from 'http://example.invalid/channel':\n"
                "  Stream #0:0: Video: h264 (High), yuv420p(tv, bt709), 1920x1080, 50 fps\n"
                "  Stream #0:1(???): Audio: eac3, 48000 Hz, 5.1(side), fltp, 448 kb/s\n"
                "  Stream #0:2(???): Audio: eac3, 48000 Hz, 5.1(side), fltp, 448 kb/s\n"
                "  Stream #0:3(???): Audio: eac3, 48000 Hz, 5.1(side), fltp, 448 kb/s\n"
                "frame= 1800 fps=50 speed=1.00x\n"
            ),
        )

    monkeypatch.setattr("app.stream_stability.subprocess.run", fake_run)

    result = run_stream_stability_test(
        "http://example.invalid/channel",
        StabilitySettings(duration_seconds=30, retries=0),
    )

    assert result.status == "GOOD"
    assert "multiple EAC3 audio tracks (3)" in result.issues


def test_h264_1080p_with_single_eac3_no_annotation(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stderr=(
                "Input #0, hls, from 'http://example.invalid/channel':\n"
                "  Stream #0:0: Video: h264 (High), yuv420p(tv, bt709), 1920x1080, 50 fps\n"
                "  Stream #0:1(???): Audio: eac3, 48000 Hz, 5.1(side), fltp, 448 kb/s\n"
                "frame= 1800 fps=50 speed=1.00x\n"
            ),
        )

    monkeypatch.setattr("app.stream_stability.subprocess.run", fake_run)

    result = run_stream_stability_test(
        "http://example.invalid/channel",
        StabilitySettings(duration_seconds=30, retries=0),
    )

    assert result.status == "GOOD"
    assert "multiple EAC3" not in result.issues


def test_hevc_4k_does_not_override_bad(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stderr=_FFMPEG_HEVC_4K_BANNER + "connection timed out\n",
        )

    monkeypatch.setattr("app.stream_stability.subprocess.run", fake_run)

    result = run_stream_stability_test(
        "http://example.invalid/channel",
        StabilitySettings(duration_seconds=30, retries=0),
    )

    assert result.status == "BAD"
    assert "connection timed out" in result.issues
    assert "4K HEVC stream, heavy client decode load" in result.issues


def test_stream_properties_stop_at_mapping_line(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stderr=(
                "Input #0, hls, from 'http://example.invalid/channel':\n"
                "  Stream #0:0: Video: h264 (High), yuv420p, 1920x1080, 50 fps\n"
                "  Stream #0:1: Audio: aac (LC)\n"
                "  Stream #0:0 -> #0:0 (h264 (native) -> wrapped_avframe (native))\n"
                "  Stream #0:1 -> #0:1 (aac (native) -> pcm_s16le (native))\n"
                "  Stream #0:0: Video: wrapped_avframe, yuv420p, 1920x1080\n"
                "frame= 1800 fps=50 speed=1.00x\n"
            ),
        )

    monkeypatch.setattr("app.stream_stability.subprocess.run", fake_run)

    result = run_stream_stability_test(
        "http://example.invalid/channel",
        StabilitySettings(duration_seconds=30, retries=0),
    )

    assert result.status == "GOOD"
    assert "4K HEVC" not in result.issues


def test_freeze_detection_triggers_bad(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stderr=(
                _FFMPEG_H264_1080P_BANNER
                + "frame= 300 fps=10 speed=1.00x\n"
                + "[Parsed_freezedetect_2 @ ...] lavfi.freezedetect.freeze_start: 5.0\n"
                + "[Parsed_freezedetect_2 @ ...] lavfi.freezedetect.freeze_duration: 4.5\n"
                + "[Parsed_freezedetect_2 @ ...] lavfi.freezedetect.freeze_end: 9.5\n"
                + "[Parsed_freezedetect_2 @ ...] lavfi.freezedetect.freeze_start: 15.0\n"
                + "[Parsed_freezedetect_2 @ ...] lavfi.freezedetect.freeze_duration: 2.0\n"
                + "[Parsed_freezedetect_2 @ ...] lavfi.freezedetect.freeze_end: 17.0\n"
            ),
        )

    monkeypatch.setattr("app.stream_stability.subprocess.run", fake_run)

    result = run_stream_stability_test(
        "http://example.invalid/channel",
        StabilitySettings(duration_seconds=30, retries=0),
    )

    assert result.status == "BAD"
    assert "video freezes" in result.issues
    assert "6.5s total" in result.issues


def test_minor_freeze_triggers_warn(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stderr=(
                _FFMPEG_H264_1080P_BANNER
                + "frame= 300 fps=10 speed=1.00x\n"
                + "[Parsed_freezedetect_2 @ ...] lavfi.freezedetect.freeze_start: 10.0\n"
                + "[Parsed_freezedetect_2 @ ...] lavfi.freezedetect.freeze_duration: 1.5\n"
                + "[Parsed_freezedetect_2 @ ...] lavfi.freezedetect.freeze_end: 11.5\n"
            ),
        )

    monkeypatch.setattr("app.stream_stability.subprocess.run", fake_run)

    result = run_stream_stability_test(
        "http://example.invalid/channel",
        StabilitySettings(duration_seconds=30, retries=0),
    )

    assert result.status == "WARN"
    assert "minor video freeze" in result.issues
    assert "1 events" in result.issues


def test_black_detection_triggers_bad(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stderr=(
                _FFMPEG_H264_1080P_BANNER
                + "frame= 300 fps=10 speed=1.00x\n"
                + "[Parsed_blackdetect_3 @ ...] black_start:3.0 black_end:7.5 black_duration:4.5\n"
            ),
        )

    monkeypatch.setattr("app.stream_stability.subprocess.run", fake_run)

    result = run_stream_stability_test(
        "http://example.invalid/channel",
        StabilitySettings(duration_seconds=30, retries=0),
    )

    assert result.status == "BAD"
    assert "black screen: 4.5s total" in result.issues


def test_black_warns_on_small_amount(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stderr=(
                _FFMPEG_H264_1080P_BANNER
                + "frame= 300 fps=10 speed=1.00x\n"
                + "[Parsed_blackdetect_3 @ ...] black_start:3.0 black_end:4.5 black_duration:1.5\n"
            ),
        )

    monkeypatch.setattr("app.stream_stability.subprocess.run", fake_run)

    result = run_stream_stability_test(
        "http://example.invalid/channel",
        StabilitySettings(duration_seconds=30, retries=0),
    )

    assert result.status == "WARN"
    assert "minor black screen: 1.5s total" in result.issues

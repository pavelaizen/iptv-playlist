from pathlib import Path

import asyncio

from app import main as app_main


def test_run_once_smoke(monkeypatch, tmp_path: Path):
    raw = tmp_path / "raw.m3u"
    raw.write_text("#EXTM3U\n#EXTINF:-1,Chan1\nhttp://ok\n#EXTINF:-1,Chan2\nhttp://bad\n", encoding="utf-8")

    out_dir = tmp_path / "out"
    state_file = out_dir / ".state"

    monkeypatch.setattr(app_main, "RAW_PLAYLIST_PATH", raw)
    monkeypatch.setattr(app_main, "OUTPUT_DIR", out_dir)
    monkeypatch.setattr(app_main, "OUTPUT_PLAYLIST_NAME", "playlist_clean.m3u")
    monkeypatch.setattr(app_main, "PREVIOUS_CLEAN_PLAYLIST_NAME", "playlist_clean_prev.m3u")
    monkeypatch.setattr(app_main, "STATE_FILE", state_file)
    monkeypatch.setattr(app_main, "DIAGNOSTICS_DIR", out_dir / "diag")

    class _Result:
        def __init__(self, channel: str, valid: bool):
            self.channel = channel
            self.valid = valid

    class _Stats:
        def as_dict(self):
            return {"total_channels": 2, "valid": 1, "invalid": 1, "timeout": 0, "retry_success": 0}

    async def fake_probe_channels(channels, settings):  # noqa: ARG001
        return [_Result("http://ok", True), _Result("http://bad", False)], _Stats()

    monkeypatch.setattr(app_main, "probe_channels", fake_probe_channels)
    monkeypatch.setattr(app_main, "refresh_livetv_after_publish", lambda logger: None)

    ok = asyncio.run(app_main.run_once())
    assert ok is True

    output = (out_dir / "playlist_clean.m3u").read_text(encoding="utf-8")
    assert "http://ok" in output
    assert "http://bad" not in output
    assert state_file.exists()

from pathlib import Path

from app.publish import PublishGuardConfig, select_playlist_for_publish


def test_publish_guard_accepts_candidate(tmp_path: Path):
    candidate = tmp_path / "candidate.m3u"
    previous = tmp_path / "previous.m3u"
    previous.write_text("#EXTM3U\n#EXTINF:-1,old\nhttp://old\n", encoding="utf-8")

    decision = select_playlist_for_publish(
        candidate_output_path=candidate,
        previous_clean_path=previous,
        candidate_content="#EXTM3U\n#EXTINF:-1,new\nhttp://new\n",
        config=PublishGuardConfig(min_valid_channels_absolute=1, min_valid_ratio_of_previous=0.5),
    )

    assert decision.publish_candidate is True
    assert candidate.read_text(encoding="utf-8").startswith("#EXTM3U")


def test_publish_guard_falls_back_and_writes_diagnostic(tmp_path: Path):
    candidate = tmp_path / "candidate.m3u"
    previous = tmp_path / "previous.m3u"
    previous.write_text("#EXTM3U\n#EXTINF:-1,old\nhttp://old\n#EXTINF:-1,old2\nhttp://old2\n", encoding="utf-8")

    decision = select_playlist_for_publish(
        candidate_output_path=candidate,
        previous_clean_path=previous,
        candidate_content="#EXTM3U\n",
        config=PublishGuardConfig(min_valid_channels_absolute=1, min_valid_ratio_of_previous=0.8, diagnostics_dir=tmp_path / "diag"),
    )

    assert decision.publish_candidate is False
    assert decision.diagnostic_path is not None
    assert decision.diagnostic_path.exists()
    assert candidate.read_text(encoding="utf-8") == previous.read_text(encoding="utf-8")

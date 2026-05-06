from pathlib import Path
import stat

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
    assert stat.S_IMODE(candidate.stat().st_mode) == 0o644


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


def test_publish_guard_removes_temp_file_after_success(tmp_path: Path):
    candidate = tmp_path / "candidate.m3u"
    previous = tmp_path / "previous.m3u"

    decision = select_playlist_for_publish(
        candidate_output_path=candidate,
        previous_clean_path=previous,
        candidate_content="#EXTM3U\n#EXTINF:-1,new\nhttp://new\n",
        config=PublishGuardConfig(min_valid_channels_absolute=1, min_valid_ratio_of_previous=0.5),
    )

    assert decision.publish_candidate is True
    assert candidate.exists()
    assert not candidate.with_suffix(".m3u.tmp").exists()


def test_publish_guard_marks_unchanged_candidate_without_rewriting(tmp_path: Path):
    candidate = tmp_path / "candidate.m3u"
    content = "#EXTM3U\n#EXTINF:-1,same\nhttp://same\n"
    candidate.write_text(content, encoding="utf-8")
    before_mtime_ns = candidate.stat().st_mtime_ns

    decision = select_playlist_for_publish(
        candidate_output_path=candidate,
        previous_clean_path=candidate,
        candidate_content=content,
        config=PublishGuardConfig(min_valid_channels_absolute=1, min_valid_ratio_of_previous=0.5),
    )

    assert decision.publish_candidate is True
    assert decision.content_changed is False
    assert candidate.read_text(encoding="utf-8") == content
    assert candidate.stat().st_mtime_ns == before_mtime_ns


def test_publish_guard_compares_change_against_destination_not_previous(tmp_path: Path):
    candidate = tmp_path / "candidate.m3u"
    previous = tmp_path / "previous.m3u"
    old_content = "#EXTM3U\n#EXTINF:-1,old\nhttp://old\n"
    new_content = "#EXTM3U\n#EXTINF:-1,new\nhttp://new\n"
    candidate.write_text(old_content, encoding="utf-8")
    previous.write_text(new_content, encoding="utf-8")

    decision = select_playlist_for_publish(
        candidate_output_path=candidate,
        previous_clean_path=previous,
        candidate_content=new_content,
        config=PublishGuardConfig(min_valid_channels_absolute=1, min_valid_ratio_of_previous=0.5),
    )

    assert decision.publish_candidate is True
    assert decision.content_changed is True
    assert candidate.read_text(encoding="utf-8") == new_content


def test_publish_guard_removes_temp_file_after_fallback(tmp_path: Path):
    candidate = tmp_path / "candidate.m3u"
    previous = tmp_path / "previous.m3u"
    previous.write_text("#EXTM3U\n#EXTINF:-1,old\nhttp://old\n", encoding="utf-8")

    decision = select_playlist_for_publish(
        candidate_output_path=candidate,
        previous_clean_path=previous,
        candidate_content="#EXTM3U\n",
        config=PublishGuardConfig(min_valid_channels_absolute=1, min_valid_ratio_of_previous=0.8),
    )

    assert decision.publish_candidate is False
    assert candidate.read_text(encoding="utf-8") == previous.read_text(encoding="utf-8")
    assert not candidate.with_suffix(".m3u.tmp").exists()

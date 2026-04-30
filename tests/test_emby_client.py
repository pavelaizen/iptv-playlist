import logging

from app import emby_client


def test_find_refresh_guide_task_prefers_scheduled_task(monkeypatch):
    config = emby_client.EmbyConfig(
        base_url="http://emby.local",
        api_key="secret",
    )

    def fake_get_emby_json(cfg, endpoint, log):  # noqa: ARG001
        assert cfg == config
        assert endpoint == "/ScheduledTasks"
        return True, [
            {"Name": "Other Task", "Key": "Other", "Id": "1"},
            {"Name": "Refresh Guide", "Key": "RefreshGuide", "Id": "guide-123"},
        ]

    monkeypatch.setattr(emby_client, "_get_emby_json", fake_get_emby_json)

    task_id, label = emby_client._find_refresh_guide_task(config, logging.getLogger("test"))

    assert task_id == "guide-123"
    assert label == "Refresh Guide"


def test_refresh_livetv_after_publish_uses_scheduled_task(monkeypatch):
    config = emby_client.EmbyConfig(
        base_url="http://emby.local",
        api_key="secret",
    )
    calls: list[str] = []

    monkeypatch.setattr(emby_client.EmbyConfig, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(
        emby_client,
        "_get_emby_json",
        lambda cfg, endpoint, log: (True, [{"Name": "Refresh Guide", "Key": "RefreshGuide", "Id": "guide-123"}]),
    )

    def fake_post_emby(cfg, endpoint, log):  # noqa: ARG001
        calls.append(endpoint)
        return True, "ok"

    monkeypatch.setattr(emby_client, "_post_emby", fake_post_emby)

    warning = emby_client.refresh_livetv_after_publish(logging.getLogger("test"))

    assert warning is None
    assert calls == ["/ScheduledTasks/Running/guide-123"]

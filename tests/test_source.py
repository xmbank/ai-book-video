from pathlib import Path

import pytest
import requests

from book_video_workbench.douyin_bridge import _resolve_item, fetch_share_page
from book_video_workbench.source import normalize_source_meta


def test_source_metrics_keep_values_and_explain_missing_fields() -> None:
    normalized = normalize_source_meta(
        {
            "aweme_id": "123",
            "title": "测试标题",
            "description": "测试描述",
            "author": "测试作者",
            "duration_ms": 65432,
            "download_url": "https://example.com/video.mp4",
            "metrics": {"like": 12000, "comment": 88, "share": 321},
        },
        duration=1,
        share_text="https://v.douyin.com/example/",
    )
    assert normalized["duration_seconds"] == 65.432
    assert normalized["description"] == "测试描述"
    assert normalized["metrics"]["like"] == {"value": 12000, "reason": None}
    assert normalized["metrics"]["play"] == {"value": None, "reason": "平台未返回"}


class _Response:
    def __init__(self, url: str, text: str) -> None:
        self.url = url
        self.text = text

    def raise_for_status(self) -> None:
        return None


class _RetrySession:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls < 3:
            raise requests.exceptions.SSLError("temporary EOF")
        return _Response("https://www.iesdouyin.com/share/video/123/", "<html>ok</html>")


def test_share_page_retries_requests_before_succeeding() -> None:
    session = _RetrySession()
    delays: list[float] = []

    final_url, html = fetch_share_page(
        session,
        "https://v.douyin.com/example/",
        user_agent="test",
        sleep=delays.append,
    )

    assert session.calls == 3
    assert delays == [1.0, 2.0]
    assert final_url.endswith("/123/")
    assert html == "<html>ok</html>"


def test_share_page_uses_curl_after_requests_fail(tmp_path: Path, monkeypatch) -> None:
    class FailedSession:
        def get(self, *_args, **_kwargs):
            raise requests.exceptions.ConnectionError("temporary disconnect")

    def fake_run(command, **_kwargs):
        output = Path(command[command.index("--output") + 1])
        output.write_text("<html>curl ok</html>", encoding="utf-8")
        return type("Proc", (), {"returncode": 0, "stdout": "https://final.example/", "stderr": ""})()

    monkeypatch.setattr("book_video_workbench.douyin_bridge.subprocess.run", fake_run)
    delays: list[float] = []

    final_url, html = fetch_share_page(
        FailedSession(),
        "https://v.douyin.com/example/",
        user_agent="test-agent",
        sleep=delays.append,
        curl_path="/usr/bin/curl",
    )

    assert delays == [1.0, 2.0, 4.0]
    assert final_url == "https://final.example/"
    assert html == "<html>curl ok</html>"


def test_share_page_failure_has_stable_diagnostic_marker(monkeypatch) -> None:
    class FailedSession:
        def get(self, *_args, **_kwargs):
            raise requests.exceptions.SSLError("temporary EOF")

    def fake_run(*_args, **_kwargs):
        return type("Proc", (), {"returncode": 35, "stdout": "", "stderr": "TLS failed"})()

    monkeypatch.setattr("book_video_workbench.douyin_bridge.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="DOUYIN_SHARE_FETCH_FAILED"):
        fetch_share_page(
            FailedSession(),
            "https://v.douyin.com/example/",
            user_agent="test",
            sleep=lambda _delay: None,
            curl_path="/usr/bin/curl",
        )


def test_share_page_without_item_data_uses_signed_detail_fallback(monkeypatch) -> None:
    class Resolver:
        @staticmethod
        def _parse_router_data(_html):
            return {"loaderData": {}}

        @staticmethod
        def _parse_render_data(_html):
            return None

        @staticmethod
        def _find_item_list(_payload):
            return []

        @staticmethod
        def extract_aweme_id(page_url, _html):
            assert page_url.endswith("/7655303058867400185/")
            return "7655303058867400185"

    expected = {"aweme_id": "7655303058867400185", "desc": "测试作品"}
    seen: list[str] = []

    def fake_detail(aweme_id: str) -> dict:
        seen.append(aweme_id)
        return expected

    monkeypatch.setattr("book_video_workbench.douyin_bridge._fetch_detail_item", fake_detail)

    item = _resolve_item(
        Resolver(),
        "<script>window._ROUTER_DATA = {}</script>",
        "https://www.iesdouyin.com/share/video/7655303058867400185/",
    )

    assert item == expected
    assert seen == ["7655303058867400185"]

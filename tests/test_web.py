from pathlib import Path

from fastapi.testclient import TestClient

from book_video_workbench.config import Settings
from book_video_workbench.pipeline import Pipeline, RunOptions, create_task
from book_video_workbench.util import read_json, write_json
import book_video_workbench.web as web


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        capture_backend_dir=tmp_path / "capture",
        llm_base_url="https://example.com/v1",
        llm_api_key="",
        llm_model="",
        image_base_url="https://example.com/v1",
        image_api_key="",
        image_model="gpt-image-2",
        image_size="1536x1024",
        volc_tts_endpoint="https://example.com/tts",
        volc_tts_api_key="",
        volc_tts_resource_id="seed-tts-2.0",
        volc_tts_voice_type="",
        reference_demo_video=tmp_path / "reference.mp4",
    )


def test_task_list_and_detail(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(web, "settings", _settings(tmp_path))
    task_dir = create_task(
        web.settings,
        RunOptions(
            mode="demo",
            share_text="",
            book_title="测试图书",
            selling_points=["真实卖点"],
        ),
    )
    write_json(
        task_dir / "source" / "meta.normalized.json",
        {"title": "测试来源", "author": "测试作者"},
    )
    client = TestClient(web.app)
    listing = client.get("/api/tasks")
    assert listing.status_code == 200
    assert listing.json()[0]["title"] == "测试来源"
    detail = client.get(f"/api/tasks/{task_dir.name}")
    assert detail.status_code == 200
    assert detail.json()["book_title"] == "测试图书"


def test_real_task_requires_link_but_not_prefilled_book_data(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(web, "settings", _settings(tmp_path))
    client = TestClient(web.app)
    response = client.post(
        "/api/tasks",
        json={"mode": "real", "share_text": "", "book_title": "书", "selling_points": []},
    )
    assert response.status_code == 422


def test_article_flow_edit_endpoints_version_and_invalidate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(web, "settings", _settings(tmp_path))
    task_dir = create_task(web.settings, RunOptions(mode="demo", share_text=""))
    repaired = write_json(
        task_dir / "transcript" / "repaired-v1.json",
        {"raw_text": "原文", "cleaned_text": "旧修复稿", "repairs": [], "findings": []},
    )
    task = read_json(task_dir / "task.json")
    task["active_artifacts"] = {"repaired_transcript": str(repaired.relative_to(task_dir))}
    write_json(task_dir / "task.json", task)
    client = TestClient(web.app)

    repaired_response = client.patch(
        f"/api/tasks/{task_dir.name}/repair", json={"cleaned_text": "新修复稿"}
    )
    assert repaired_response.status_code == 200
    assert repaired_response.json()["repaired_transcript"]["cleaned_text"] == "新修复稿"

    book_response = client.patch(
        f"/api/tasks/{task_dir.name}/book",
        json={"book_title": "测试书", "book_author": "测试作者", "confidence": 1},
    )
    assert book_response.status_code == 200
    assert book_response.json()["book_info"]["needs_review"] is False

    style_response = client.patch(
        f"/api/tasks/{task_dir.name}/styles",
        json={
            "styles": ["clean-narration", "typewriter-dark"],
            "style_counts": {"clean-narration": 1, "typewriter-dark": 2},
            "declaration": "仅作阅读分享。",
            "scene_count": 18,
        },
    )
    assert style_response.status_code == 200
    options = read_json(task_dir / "task.json")["options"]
    assert options["style_counts"]["typewriter-dark"] == 2


def test_style_stage_expands_per_style_output_counts(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    task_dir = create_task(
        settings,
        RunOptions(
            mode="demo",
            share_text="",
            styles=["clean-narration", "typewriter-dark"],
            style_counts={"clean-narration": 1, "typewriter-dark": 2},
        ),
    )
    pipeline = Pipeline(task_dir, settings)
    pipeline._stage_styles()
    style_config = read_json(pipeline._active("style_config"))
    assert style_config["counts"] == {"clean-narration": 1, "typewriter-dark": 2}
    assert style_config["output_count"] == 3


def test_task_detail_sanitizes_legacy_failure_diagnostics(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(web, "settings", _settings(tmp_path))
    task_dir = create_task(web.settings, RunOptions(mode="demo", share_text=""))
    state_path = task_dir / "pipeline-state.json"
    state = read_json(state_path)
    state["stages"]["repair"].update(
        {
            "status": "failed",
            "message": "命令失败 (1): /Users/example/.venv/bin/python",
            "error": {
                "type": "RuntimeError",
                "message": "命令失败\nrequests.exceptions.SSLError: UNEXPECTED_EOF_WHILE_READING",
                "traceback": "Traceback /Users/example/site-packages/requests/adapters.py",
            },
        }
    )
    write_json(state_path, state)

    response = TestClient(web.app).get(f"/api/tasks/{task_dir.name}")

    assert response.status_code == 200
    body = response.json()
    failure = body["stages"]["repair"]
    assert failure["error"] == {
        "type": "RuntimeError",
        "message": "抖音短链连接被临时中断，系统已自动重试。请稍后点击“重试”。",
        "retryable": True,
    }
    serialized = response.text.lower()
    assert "traceback" not in serialized
    assert "site-packages" not in serialized
    assert "/users/example" not in serialized

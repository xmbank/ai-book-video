from pathlib import Path
import base64
import io

from fastapi.testclient import TestClient
from PIL import Image

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


def test_book_update_persists_product_inputs_and_invalidates_rewrite(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(web, "settings", _settings(tmp_path))
    task_dir = create_task(web.settings, RunOptions(mode="demo", share_text=""))
    identified = write_json(
        task_dir / "book" / "identity-v1.json",
        {"suggested_selling_points": ["候选内容卖点一", "候选内容卖点二"]},
    )
    task_record = read_json(task_dir / "task.json")
    task_record["active_artifacts"] = {
        "book_info": str(identified.relative_to(task_dir))
    }
    write_json(task_dir / "task.json", task_record)
    cover = tmp_path / "real-cover.jpg"
    cover.write_bytes(b"real cover fixture")
    state_path = task_dir / "pipeline-state.json"
    state = read_json(state_path)
    for name in ("rewrite", "audio", "scene_images", "styles", "outputs", "review"):
        state["stages"][name]["status"] = "succeeded"
    write_json(state_path, state)

    response = TestClient(web.app).patch(
        f"/api/tasks/{task_dir.name}/book",
        json={
            "book_title": "黄帝内经",
            "book_author": "人民卫生出版社整理版",
            "confidence": 1,
            "selling_points": ["白话注释", "原文与译文对照"],
            "book_cover": str(cover),
            "rewrite_mode": "deep",
            "target_seconds": 45,
        },
    )

    assert response.status_code == 200
    book = response.json()["book_info"]
    assert book["selling_points"] == ["白话注释", "原文与译文对照"]
    assert book["suggested_selling_points"] == ["候选内容卖点一", "候选内容卖点二"]
    assert book["book_cover"] == str(cover.resolve())
    assert book["product_ready"] is True
    options = read_json(task_dir / "task.json")["options"]
    assert options["selling_points"] == ["白话注释", "原文与译文对照"]
    assert options["book_cover"] == str(cover.resolve())
    assert options["rewrite_mode"] == "deep"
    assert options["target_seconds"] == 45
    persisted_state = read_json(state_path)["stages"]
    assert persisted_state["book_info"]["status"] == "succeeded"
    assert all(
        persisted_state[name]["status"] == "stale"
        for name in ("rewrite", "audio", "scene_images", "styles", "outputs", "review")
    )


def test_book_cover_upload_validates_image_and_can_complete_product_assets(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(web, "settings", _settings(tmp_path))
    task_dir = create_task(web.settings, RunOptions(mode="demo", share_text=""))
    buffer = io.BytesIO()
    Image.new("RGB", (600, 900), (210, 64, 48)).save(buffer, format="JPEG")
    data_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()
    client = TestClient(web.app)

    upload = client.post(
        f"/api/tasks/{task_dir.name}/book-cover",
        json={"filename": "抗老生活.jpg", "data_url": data_url},
    )

    assert upload.status_code == 200
    cover_path = Path(upload.json()["path"])
    assert cover_path.is_file()
    assert cover_path.parent == task_dir / "book" / "assets"
    with Image.open(cover_path) as cover:
        assert cover.size == (600, 900)

    saved = client.patch(
        f"/api/tasks/{task_dir.name}/book",
        json={
            "book_title": "抗老生活",
            "selling_points": ["关注日常习惯", "提供生活方式观察"],
            "book_cover": str(cover_path),
        },
    )
    assert saved.status_code == 200
    assert saved.json()["book_info"]["product_ready"] is True


def test_book_cover_upload_rejects_mismatched_content_type(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(web, "settings", _settings(tmp_path))
    task_dir = create_task(web.settings, RunOptions(mode="demo", share_text=""))
    buffer = io.BytesIO()
    Image.new("RGB", (300, 400), (30, 80, 160)).save(buffer, format="PNG")
    data_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()

    response = TestClient(web.app).post(
        f"/api/tasks/{task_dir.name}/book-cover",
        json={"filename": "cover.jpg", "data_url": data_url},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "封面文件类型与图片内容不一致"


def test_ai_book_cover_endpoint_generates_task_local_asset(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(web, "settings", _settings(tmp_path))
    task_dir = create_task(web.settings, RunOptions(mode="demo", share_text=""))

    def fake_generate(*, output_path, metadata_path, **kwargs):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (900, 1350), (36, 98, 112)).save(output_path)
        write_json(metadata_path, {"book_title": kwargs["book_title"]})
        return output_path, metadata_path

    monkeypatch.setattr(web, "generate_ai_book_cover", fake_generate)
    response = TestClient(web.app).post(
        f"/api/tasks/{task_dir.name}/book-cover/generate",
        json={
            "book_title": "身体重置",
            "book_author": "测试作者",
            "selling_points": ["蛋白质配速", "彩虹饮食"],
        },
    )

    assert response.status_code == 200
    generated = Path(response.json()["path"])
    assert generated.is_file()
    assert generated.parent == task_dir / "book" / "assets"
    assert response.json()["source"] == "ai_generated"


def test_style_primary_visual_change_invalidates_scene_images_and_allows_auto_count(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(web, "settings", _settings(tmp_path))
    task_dir = create_task(
        web.settings,
        RunOptions(
            mode="demo",
            share_text="",
            scene_count=18,
            styles=["clean-narration"],
            style_counts={"clean-narration": 1},
        ),
    )
    state_path = task_dir / "pipeline-state.json"
    state = read_json(state_path)
    for name in ("scene_images", "styles", "outputs", "review"):
        state["stages"][name]["status"] = "succeeded"
    write_json(state_path, state)

    response = TestClient(web.app).patch(
        f"/api/tasks/{task_dir.name}/styles",
        json={
            "styles": ["book-sales"],
            "style_counts": {"book-sales": 1},
            "declaration": "仅作阅读分享。",
            "scene_count": 0,
        },
    )

    assert response.status_code == 200
    assert response.json()["options"]["scene_count"] == 0
    stages = read_json(state_path)["stages"]
    assert all(
        stages[name]["status"] == "stale"
        for name in ("scene_images", "styles", "outputs", "review")
    )


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


def test_task_detail_reclassifies_legacy_scene_timeout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(web, "settings", _settings(tmp_path))
    task_dir = create_task(web.settings, RunOptions(mode="demo", share_text=""))
    state_path = task_dir / "pipeline-state.json"
    state = read_json(state_path)
    state["stages"]["scene_images"].update(
        {
            "status": "failed",
            "message": "抖音短链连接被临时中断，系统已自动重试。请稍后点击“重试”。",
            "error": {
                "type": "TimeoutError",
                "technical_message": "The read operation timed out",
            },
        }
    )
    write_json(state_path, state)

    response = TestClient(web.app).get(f"/api/tasks/{task_dir.name}")

    assert response.status_code == 200
    failure = response.json()["stages"]["scene_images"]
    assert failure["message"] == (
        "竖屏视觉分镜服务连接暂时中断，系统已自动重试。"
        "已生成的场景图会保留，请直接重试本步骤。"
    )
    assert failure["error"]["retryable"] is True

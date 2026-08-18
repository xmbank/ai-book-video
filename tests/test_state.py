from pathlib import Path

import pytest

from book_video_workbench.state import PipelineState
from book_video_workbench.util import write_json


def test_stage_success_records_artifact_and_invalidation(tmp_path: Path) -> None:
    state = PipelineState(tmp_path)
    artifact = tmp_path / "source" / "meta.json"
    with state.running("repair") as outputs:
        artifact.parent.mkdir(parents=True)
        artifact.write_text("{}", encoding="utf-8")
        outputs.append(artifact)
    assert state.stage_complete("repair")
    state.invalidate_from("repair")
    assert state.data["stages"]["repair"]["status"] == "stale"


def test_legacy_pipeline_state_migrates_to_article_stages(tmp_path: Path) -> None:
    write_json(
        tmp_path / "pipeline-state.json",
        {
            "schema_version": 1,
            "stages": {
                "source": {"status": "succeeded", "artifacts": ["source/meta.json"]},
                "transcript": {"status": "succeeded", "artifacts": ["transcript/v1.json"]},
                "rewrite": {"status": "succeeded", "artifacts": ["scripts/v1.json"]},
                "tts": {"status": "succeeded", "artifacts": ["tts/v1.wav"]},
                "subtitles": {"status": "succeeded", "artifacts": ["subtitles/v1.json"]},
                "storyboard": {"status": "succeeded", "artifacts": ["storyboard/v1.json"]},
                "render": {"status": "succeeded", "artifacts": ["renders/v1.mp4"]},
                "validate": {"status": "succeeded", "artifacts": ["renders/v1.json"]},
            },
        },
    )
    state = PipelineState(tmp_path)
    assert list(state.data["stages"]) == [
        "repair", "book_info", "rewrite", "audio", "scene_images", "styles", "outputs", "review"
    ]
    assert state.data["stages"]["repair"]["status"] == "succeeded"
    assert state.data["stages"]["scene_images"]["status"] == "stale"
    assert "legacy_stages" in state.data


def test_existing_article_state_is_reordered_without_losing_records(tmp_path: Path) -> None:
    old_order = [
        "repair",
        "rewrite",
        "audio",
        "scene_images",
        "book_info",
        "styles",
        "outputs",
        "review",
    ]
    write_json(
        tmp_path / "pipeline-state.json",
        {
            "schema_version": 2,
            "stages": {
                name: {
                    "status": "succeeded",
                    "artifacts": [f"artifacts/{name}.json"],
                    "message": f"保留 {name}",
                }
                for name in old_order
            },
        },
    )

    state = PipelineState(tmp_path)

    assert list(state.data["stages"]) == [
        "repair",
        "book_info",
        "rewrite",
        "audio",
        "scene_images",
        "styles",
        "outputs",
        "review",
    ]
    assert state.data["schema_version"] == 3
    assert state.data["stages"]["book_info"]["artifacts"] == [
        "artifacts/book_info.json"
    ]
    assert state.data["stages"]["rewrite"]["message"] == "保留 rewrite"


def test_stage_failure_keeps_diagnostics_but_stores_public_message(tmp_path: Path) -> None:
    state = PipelineState(tmp_path)
    technical = "DOUYIN_SHARE_FETCH_FAILED\nrequests attempt 1: SSLError"

    with pytest.raises(RuntimeError):
        with state.running("repair"):
            raise RuntimeError(technical)

    failed = state.data["stages"]["repair"]
    assert failed["message"] == "抖音短链连接被临时中断，系统已自动重试。请稍后点击“重试”。"
    assert failed["error"]["message"] == failed["message"]
    assert failed["error"]["retryable"] is True
    assert failed["error"]["technical_message"] == technical
    assert "RuntimeError" in failed["error"]["traceback"]


def test_image_disconnect_uses_image_specific_public_message(tmp_path: Path) -> None:
    state = PipelineState(tmp_path)

    with pytest.raises(RuntimeError):
        with state.running("scene_images"):
            raise RuntimeError(
                "IMAGE_GENERATION_TRANSIENT_ERROR: 图片生成连接连续中断"
            )

    failed = state.data["stages"]["scene_images"]
    assert failed["message"] == (
        "图片生成服务连接暂时中断，系统已自动重试。"
        "已生成的竖屏图会保留，请稍后点击“重试”继续。"
    )
    assert failed["error"]["retryable"] is True


@pytest.mark.parametrize(
    "technical",
    [
        "TTS_TRANSIENT_ERROR: 豆包 TTS 网络请求连续失败: TLS disconnected",
        "豆包 TTS 网络请求失败: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred",
    ],
)
def test_tts_disconnect_uses_tts_specific_public_message(
    tmp_path: Path, technical: str
) -> None:
    state = PipelineState(tmp_path)

    with pytest.raises(RuntimeError):
        with state.running("audio"):
            raise RuntimeError(technical)

    failed = state.data["stages"]["audio"]
    assert failed["message"] == (
        "豆包配音连接暂时中断，系统已自动重试。"
        "已生成的音频片段会保留，请直接重试本步骤。"
    )
    assert failed["error"]["retryable"] is True


def test_scene_director_timeout_uses_visual_specific_public_message(
    tmp_path: Path,
) -> None:
    state = PipelineState(tmp_path)

    with pytest.raises(TimeoutError):
        with state.running("scene_images"):
            raise TimeoutError("The read operation timed out")

    failed = state.data["stages"]["scene_images"]
    assert failed["message"] == (
        "竖屏视觉分镜服务连接暂时中断，系统已自动重试。"
        "已生成的场景图会保留，请直接重试本步骤。"
    )
    assert failed["error"]["retryable"] is True


def test_missing_product_assets_uses_actionable_public_message(tmp_path: Path) -> None:
    state = PipelineState(tmp_path)

    with pytest.raises(RuntimeError):
        with state.running("scene_images"):
            raise RuntimeError(
                "PRODUCT_ASSETS_REQUIRED: 正式带货生图前，请先确认真实封面和至少 2 条商品卖点。"
            )

    failed = state.data["stages"]["scene_images"]
    assert failed["message"] == (
        "商品资料流程已改为自动模式：系统会采用来源中提取的卖点并生成 AI 概念封面。"
        "请直接重试本步骤。"
    )
    assert failed["error"]["retryable"] is True

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
        "repair", "rewrite", "audio", "scene_images", "book_info", "styles", "outputs", "review"
    ]
    assert state.data["stages"]["repair"]["status"] == "succeeded"
    assert state.data["stages"]["scene_images"]["status"] == "stale"
    assert "legacy_stages" in state.data


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

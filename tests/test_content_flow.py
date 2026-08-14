from pathlib import Path
import json

from book_video_workbench.article_prompts import (
    BOOK_SYSTEM_PROMPT,
    REPAIR_SYSTEM_PROMPT,
    REWRITE_SYSTEM_PROMPT,
    TTS_SEGMENT_SYSTEM_PROMPT,
)
from book_video_workbench.config import Settings
import book_video_workbench.content_flow as content_flow
from book_video_workbench.content_flow import (
    DEMO_RAW_TRANSCRIPT,
    compliance_findings,
    identify_book,
    repair_transcript,
    rewrite_candidates,
    scene_briefs,
    split_narration,
    split_narration_with_article_prompt,
)


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


def test_demo_repair_removes_marketing_noise(tmp_path: Path) -> None:
    path = repair_transcript(
        DEMO_RAW_TRANSCRIPT,
        keyword="时间管理",
        title="title",
        author="author",
        settings=_settings(tmp_path),
        output_path=tmp_path / "repaired.json",
        demo=True,
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    assert "点赞关注" not in value["cleaned_text"]
    assert "把时间当作朋友" in value["cleaned_text"]


def test_book_identity_marks_confident_demo_as_ready(tmp_path: Path) -> None:
    path = identify_book(
        "《把时间当作朋友》讨论时间与成长。",
        existing_title="",
        existing_author="",
        keyword="时间管理",
        source_title="",
        source_description="",
        settings=_settings(tmp_path),
        output_path=tmp_path / "book.json",
        demo=True,
    )
    assert '"needs_review": false' in path.read_text(encoding="utf-8")


def test_tts_segments_and_scene_briefs_cover_long_script() -> None:
    script = "第一段内容。" * 80
    assert len(split_narration(script, max_chars=100)) > 4
    briefs = scene_briefs(script, 18)
    assert len(briefs) == 18
    assert all("不要画" in item["safety_translation"] for item in briefs)
    assert briefs[0]["visual_brief"] == "第一段内容。"
    assert "阅读、书桌、散步" not in briefs[0]["visual_brief"]


def test_compliance_scanner_flags_medical_promises() -> None:
    findings = compliance_findings("这本书保证康复，是最有效的方法，私信我购买。")
    assert {item["category"] for item in findings} == {"医疗承诺", "极限表达", "导流诱导"}


def test_real_repair_uses_article_appendix_a_verbatim(tmp_path: Path, monkeypatch) -> None:
    captured = {}

    def fake_chat_text(settings, *, system, user, temperature):
        captured.update(system=system, user=user, temperature=temperature)
        return "修复后的纯正文。"

    monkeypatch.setattr(content_flow, "_chat_text", fake_chat_text)
    path = repair_transcript(
        "原始逐字稿。",
        keyword="健康图书",
        title="原视频标题",
        author="原作者",
        settings=_settings(tmp_path),
        output_path=tmp_path / "repaired.json",
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    assert captured["system"] == REPAIR_SYSTEM_PROMPT
    assert "请对下面的逐字稿做修复型清洗。" in captured["user"]
    assert "原始逐字稿。" in captured["user"]
    assert value["prompt"]["system"] == REPAIR_SYSTEM_PROMPT


def test_real_rewrite_calls_article_appendix_b_three_times(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_chat_text(settings, *, system, user, temperature):
        calls.append((system, user, temperature))
        return f"这是第{len(calls)}版口播文案。后续内容。"

    monkeypatch.setattr(content_flow, "_chat_text", fake_chat_text)
    path = rewrite_candidates(
        "清洗后的正文。",
        keyword="图书带货",
        title="标题",
        author="作者",
        notes="保留爆点",
        settings=_settings(tmp_path),
        output_path=tmp_path / "candidates.json",
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    assert len(calls) == 3
    assert all(system == REWRITE_SYSTEM_PROMPT for system, _, _ in calls)
    assert all(temperature == 0.7 for _, _, temperature in calls)
    assert len(value["candidates"]) == 3
    assert value["prompt"]["system"] == REWRITE_SYSTEM_PROMPT


def test_real_book_identity_uses_article_appendix_d(tmp_path: Path, monkeypatch) -> None:
    captured = {}

    def fake_chat_json(settings, *, system, user, temperature):
        captured.update(system=system, user=user, temperature=temperature)
        return {
            "book_title": "测试书",
            "book_author": "测试作者",
            "confidence": 0.9,
            "evidence": "正文明确提到",
        }

    monkeypatch.setattr(content_flow, "_chat_json", fake_chat_json)
    path = identify_book(
        "逐字稿正文。",
        existing_title="",
        existing_author="",
        keyword="图书",
        source_title="标题",
        source_description="描述",
        settings=_settings(tmp_path),
        output_path=tmp_path / "book.json",
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    assert captured["system"] == BOOK_SYSTEM_PROMPT
    assert "作者名需要基于书名去联网搜索。" in captured["user"]
    assert captured["temperature"] == 0.05
    assert value["prompt"]["system"] == BOOK_SYSTEM_PROMPT


def test_real_tts_split_uses_article_appendix_f(tmp_path: Path, monkeypatch) -> None:
    captured = {}

    def fake_chat_json(settings, *, system, user, temperature):
        captured.update(system=system, user=user, temperature=temperature)
        return {"segments": ["第一段。", "第二段。"]}

    monkeypatch.setattr(content_flow, "_chat_json", fake_chat_json)
    segments, prompt = split_narration_with_article_prompt(
        "第一段。第二段。",
        keyword="图书",
        title="标题",
        author="作者",
        settings=_settings(tmp_path),
    )
    assert segments == ["第一段。", "第二段。"]
    assert captured["system"] == TTS_SEGMENT_SYSTEM_PROMPT
    assert "目标单段时长：26 秒以内" in captured["user"]
    assert prompt["system"] == TTS_SEGMENT_SYSTEM_PROMPT

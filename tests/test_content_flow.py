from pathlib import Path
import json

from book_video_workbench.article_prompts import (
    BOOK_SYSTEM_PROMPT,
    CONTENT_CARD_SYSTEM_PROMPT,
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
    direct_scene_briefs,
    repair_transcript,
    rewrite_candidates,
    score_copy_candidate,
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


def test_llm_request_retries_transient_tls_disconnects(monkeypatch) -> None:
    calls: list[int] = []
    delays: list[float] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"ok":true}'

    def fake_urlopen(_request, timeout):
        assert timeout == 180
        calls.append(1)
        if len(calls) < 3:
            raise content_flow.urllib.error.URLError("temporary TLS EOF")
        return Response()

    monkeypatch.setattr(content_flow.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(content_flow.time, "sleep", delays.append)

    body = content_flow._read_llm_response(
        content_flow.urllib.request.Request("https://example.com/v1/chat/completions")
    )

    assert body == b'{"ok":true}'
    assert len(calls) == 3
    assert delays == [1.0, 3.0]


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
    assert briefs[0]["visual_brief"] != briefs[0]["script_text"]
    assert len({item["visual_brief"] for item in briefs}) == 18
    assert {item["shot_role"] for item in briefs} >= {
        "pattern_interrupt", "human_action", "product_space", "closing"
    }


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
    assert "请对下面的逐字稿做校对型清洗。" in captured["user"]
    assert "原始逐字稿。" in captured["user"]
    assert value["prompt"]["system"] == REPAIR_SYSTEM_PROMPT


def test_real_rewrite_uses_three_distinct_strategies_and_quality_scores(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_chat_json(settings, *, system, user, temperature):
        assert system == CONTENT_CARD_SYSTEM_PROMPT
        return {
            "core_claim": "漫画版降低古文阅读门槛",
            "target_audience": "读不懂古文的年轻读者",
            "source_facts": ["漫画版更容易理解"],
            "claims_needing_evidence": [],
            "product_reasons": ["漫画图解"],
            "source_phrases_to_avoid": ["真的建议大家"],
            "recommended_focus": "阅读门槛",
        }

    def fake_chat_text(settings, *, system, user, temperature):
        calls.append((system, user, temperature))
        return f"如果你被古文劝退，这是第{len(calls)}种重新理解这本书的方式。漫画图解让阅读入口更轻松。"

    monkeypatch.setattr(content_flow, "_chat_json", fake_chat_json)
    monkeypatch.setattr(content_flow, "_chat_text", fake_chat_text)
    path = rewrite_candidates(
        "清洗后的正文。",
        keyword="图书带货",
        title="标题",
        author="作者",
        notes="保留爆点",
        settings=_settings(tmp_path),
        output_path=tmp_path / "candidates.json",
        book_title="测试书",
        selling_points=["漫画图解"],
        target_seconds=30,
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    assert len(calls) == 3
    assert all(system == REWRITE_SYSTEM_PROMPT for system, _, _ in calls)
    assert all(temperature == 0.7 for _, _, temperature in calls)
    assert "创作策略：反常识冲突" in calls[0][1]
    assert "创作策略：人群痛点" in calls[1][1]
    assert "创作策略：商品解决方案" in calls[2][1]
    assert len(value["candidates"]) == 3
    assert {item["id"] for item in value["candidates"]} == {"A", "B", "C"}
    assert all("overall_score" in item["quality"] for item in value["candidates"])
    assert value["recommended_candidate_id"] in {"A", "B", "C"}
    assert value["content_card"]["core_claim"] == "漫画版降低古文阅读门槛"


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
    assert "只能根据已提供资料判断" in captured["user"]
    assert captured["temperature"] == 0.05
    assert value["prompt"]["system"] == BOOK_SYSTEM_PROMPT


def test_visual_director_keeps_briefs_unique_when_model_repeats_shots(
    tmp_path: Path, monkeypatch
) -> None:
    repeated = {
        "narration": "同一句口播。",
        "shot_role": "editorial_symbol",
        "visual_purpose": "解释抽象观点",
        "subject": "纸张和树影",
        "action": "光影移动",
        "location": "阅读空间",
        "framing": "9:16 中景",
        "lighting": "自然光",
        "continuity": "同一材质",
        "avoid": "文字和假封面",
    }

    def fake_chat_json(settings, *, system, user, temperature):
        return {"shots": [repeated.copy() for _ in range(6)]}

    monkeypatch.setattr(content_flow, "_chat_json", fake_chat_json)
    briefs, _ = direct_scene_briefs(
        "同一句口播。" * 6,
        count=6,
        book_title="测试书",
        book_author="测试作者",
        selling_points=["真实卖点"],
        cover_available=False,
        settings=_settings(tmp_path),
    )

    assert len({item["visual_brief"] for item in briefs}) == 6
    assert briefs[0]["visual_brief"].startswith("全片镜头 1/6")
    assert briefs[-1]["visual_brief"].startswith("全片镜头 6/6")


def test_copy_quality_rewards_new_structure_and_product_specificity() -> None:
    source = "这本书真的建议大家读一读。它很容易读懂，真的值得好好看看。"
    weak = score_copy_candidate(
        source,
        source,
        book_title="测试书",
        selling_points=["漫画图解"],
        target_seconds=20,
    )
    strong = score_copy_candidate(
        source,
        "如果你一直被古文劝退，《测试书》的漫画图解提供了一个更轻松的阅读入口。",
        book_title="测试书",
        selling_points=["漫画图解"],
        target_seconds=20,
    )
    assert strong["originality_score"] > weak["originality_score"]
    assert strong["product_score"] > weak["product_score"]


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

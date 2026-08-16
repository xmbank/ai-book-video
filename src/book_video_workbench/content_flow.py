from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from book_video_workbench.article_prompts import (
    BOOK_PROMPT_VERSION,
    BOOK_SYSTEM_PROMPT,
    BOOK_USER_PROMPT,
    CONTENT_CARD_PROMPT_VERSION,
    CONTENT_CARD_SYSTEM_PROMPT,
    CONTENT_CARD_USER_PROMPT,
    REPAIR_PROMPT_VERSION,
    REPAIR_SYSTEM_PROMPT,
    REPAIR_USER_PROMPT,
    REWRITE_PROMPT_VERSION,
    REWRITE_SYSTEM_PROMPT,
    REWRITE_USER_PROMPT,
    SCENE_DIRECTOR_PROMPT_VERSION,
    SCENE_DIRECTOR_SYSTEM_PROMPT,
    SCENE_DIRECTOR_USER_PROMPT,
    TTS_SEGMENT_PROMPT_VERSION,
    TTS_SEGMENT_SYSTEM_PROMPT,
    TTS_SEGMENT_USER_PROMPT,
    json_for_prompt,
    prompt_snapshot,
    render_prompt,
)
from book_video_workbench.config import Settings
from book_video_workbench.util import write_json


DEMO_RAW_TRANSCRIPT = """大家好欢迎来到我的频道，今天讲一本真正能改变时间观念的书。很多人以为时间管理就是把一天塞得更满，列更多计划，买更多效率工具。可忙到最后，真正重要的事情还是没有推进。把时间当作朋友这本书提醒我们，时间不会因为焦虑而变多，成长也不会因为着急就提前发生。一个人真正能依靠的，是接受现实，知道自己的能力边界，然后把有限的注意力放到长期积累上。学习、健康、工作都是这样。今天读十页，明天走半小时，后天认真完成一个小任务，这些事情看起来不惊人，可只要方向正确，重复足够久，就会形成别人很难追上的积累。书里还讲到一个很重要的观点，很多问题不是时间不够，而是我们总想立刻得到结果。越想走捷径，越容易在选择和后悔之间消耗时间。真正有效的做法，是先判断什么值得长期做，再给它足够耐心。如果你总觉得自己起步太晚，或者计划很多却坚持不下去，可以读一读这本书。它不会替你安排每一分钟，却能帮你重新理解时间、耐心和成长。喜欢的话记得点赞关注，我们下期再见。"""

DEMO_CLEANED_TRANSCRIPT = """很多人以为时间管理就是把一天塞得更满，列更多计划，买更多效率工具。可忙到最后，真正重要的事情还是没有推进。《把时间当作朋友》提醒我们，时间不会因为焦虑而变多，成长也不会因为着急就提前发生。一个人真正能依靠的，是接受现实，知道自己的能力边界，然后把有限的注意力放到长期积累上。学习、健康、工作都是这样。今天读十页，明天走半小时，后天认真完成一个小任务，这些事情看起来不惊人，可只要方向正确，重复足够久，就会形成别人很难追上的积累。书里还讲到一个很重要的观点，很多问题不是时间不够，而是我们总想立刻得到结果。越想走捷径，越容易在选择和后悔之间消耗时间。真正有效的做法，是先判断什么值得长期做，再给它足够耐心。如果你总觉得自己起步太晚，或者计划很多却坚持不下去，可以读一读这本书。它不会替你安排每一分钟，却能帮你重新理解时间、耐心和成长。"""

RISK_PATTERNS = {
    "医疗承诺": ["治愈", "根治", "药到病除", "保证康复", "替代药物"],
    "极限表达": ["百分之百", "绝对", "第一", "最有效", "唯一"],
    "恐吓逼单": ["不看就晚了", "再不买", "后悔一辈子"],
    "导流诱导": ["私信我", "评论区扣", "点我主页", "加微信"],
}

REWRITE_STRATEGIES = (
    {
        "id": "A",
        "label": "反常识冲突",
        "instruction": (
            "从来源里选择一个最能推翻常见误解、但有资料支撑的观点开场。"
            "先呈现误解与反差，再揭示书和商品如何帮助读者理解；不得堆砌多个禁忌。"
        ),
    },
    {
        "id": "B",
        "label": "人群痛点",
        "instruction": (
            "锁定一个具体读者场景和阅读障碍开场，例如想了解某主题却被古文、理论或篇幅劝退。"
            "围绕这个人群的一次认知变化推进，不沿用来源信息顺序。"
        ),
    },
    {
        "id": "C",
        "label": "商品解决方案",
        "instruction": (
            "尽早展示具体图书解决了什么阅读问题，使用已确认卖点解释为什么选择这个版本。"
            "没有商品资料时明确聚焦阅读入口，不得编造规格或内页。"
        ),
    },
)

SHOT_ROLES = (
    "pattern_interrupt",
    "establishing",
    "human_action",
    "detail",
    "editorial_symbol",
    "product_space",
    "transition",
    "closing",
)

LLM_NETWORK_RETRY_DELAYS = (1.0, 3.0, 8.0)


def _read_llm_response(request: urllib.request.Request) -> bytes:
    """Read an LLM response, retrying transient TLS/proxy disconnects."""
    for attempt, delay in enumerate((*LLM_NETWORK_RETRY_DELAYS, None)):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError):
            if delay is None:
                raise
            time.sleep(delay)
    raise AssertionError("unreachable")


def _chat_json(
    settings: Settings,
    *,
    system: str,
    user: str,
    temperature: float,
) -> dict[str, Any]:
    if not settings.llm_api_key or not settings.llm_model:
        raise RuntimeError("真实内容处理需要配置 LLM_API_KEY 和 LLM_MODEL")
    payload = {
        "model": settings.llm_model,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    request = urllib.request.Request(
        f"{settings.llm_base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        raw = json.loads(_read_llm_response(request).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"LLM 请求失败 ({exc.code}): {body[:1000]}") from exc
    content = raw["choices"][0]["message"]["content"].strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError("LLM 响应不包含 JSON 对象")
    return json.loads(content[start : end + 1])


def _chat_text(
    settings: Settings,
    *,
    system: str,
    user: str,
    temperature: float,
) -> str:
    if not settings.llm_api_key or not settings.llm_model:
        raise RuntimeError("真实内容处理需要配置 LLM_API_KEY 和 LLM_MODEL")
    payload = {
        "model": settings.llm_model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    request = urllib.request.Request(
        f"{settings.llm_base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        raw = json.loads(_read_llm_response(request).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"LLM 请求失败 ({exc.code}): {body[:1000]}") from exc
    content = str(raw["choices"][0]["message"]["content"]).strip()
    content = re.sub(r"^```(?:text|markdown)?\s*|\s*```$", "", content).strip()
    if not content:
        raise RuntimeError("LLM 响应为空")
    return content


def compliance_findings(text: str) -> list[dict[str, Any]]:
    findings = []
    for category, phrases in RISK_PATTERNS.items():
        matched = [phrase for phrase in phrases if phrase in text]
        if matched:
            findings.append({"category": category, "matches": matched, "severity": "high"})
    return findings


def _normalize_copy(text: str) -> str:
    return re.sub(r"[\s，。；：、“”《》！？,.!?;:'\"—…（）()\[\]【】]", "", text).lower()


def _ngram_reuse_ratio(source: str, target: str, *, size: int = 4) -> float:
    source_value = _normalize_copy(source)
    target_value = _normalize_copy(target)
    if len(target_value) < size:
        return 0.0
    source_grams = {
        source_value[index : index + size]
        for index in range(max(0, len(source_value) - size + 1))
    }
    target_grams = [
        target_value[index : index + size]
        for index in range(len(target_value) - size + 1)
    ]
    return sum(item in source_grams for item in target_grams) / max(1, len(target_grams))


def copy_similarity_metrics(source: str, target: str) -> dict[str, float]:
    source_value = _normalize_copy(source)
    target_value = _normalize_copy(target)
    return {
        "sequence_similarity": round(
            SequenceMatcher(None, source_value, target_value).ratio(), 3
        ),
        "phrase_reuse_ratio": round(_ngram_reuse_ratio(source, target), 3),
    }


def _first_sentence(text: str) -> str:
    return next(
        (
            item.strip()
            for item in re.split(r"(?<=[。！？；])", text)
            if item.strip()
        ),
        text[:36],
    )


def score_copy_candidate(
    source: str,
    script: str,
    *,
    book_title: str,
    selling_points: list[str],
    target_seconds: int,
) -> dict[str, Any]:
    similarities = copy_similarity_metrics(source, script)
    findings = compliance_findings(script)
    hook = _first_sentence(script)
    hook_concrete = any(
        marker in hook
        for marker in ("如果", "很多人", "不是", "为什么", "原来", "却", "？", "别")
    )
    hook_score = 90 if 8 <= len(hook) <= 42 and hook_concrete else 65 if len(hook) <= 48 else 45
    originality_score = round(
        max(
            0.0,
            100
            - similarities["sequence_similarity"] * 55
            - similarities["phrase_reuse_ratio"] * 45,
        )
    )
    product_hits = int(bool(book_title and book_title in script))
    product_hits += sum(point in script for point in selling_points if point)
    product_target = max(1, 1 + min(2, len(selling_points)))
    product_score = round(min(100, 50 + 50 * product_hits / product_target))
    estimated_seconds = len(script) / 4.2
    duration_delta = abs(estimated_seconds - target_seconds) / max(1, target_seconds)
    duration_score = round(max(0, 100 - duration_delta * 100))
    safety_score = max(0, 100 - 25 * len(findings))
    generic_penalty = 8 * sum(
        phrase in script
        for phrase in ("真的建议大家", "不得不说", "老祖宗的智慧", "值得好好看看")
    )
    overall = round(
        hook_score * 0.24
        + originality_score * 0.28
        + product_score * 0.22
        + duration_score * 0.11
        + safety_score * 0.15
        - generic_penalty
    )
    return {
        "overall_score": max(0, min(100, overall)),
        "hook_score": hook_score,
        "originality_score": originality_score,
        "product_score": product_score,
        "duration_score": duration_score,
        "safety_score": safety_score,
        **similarities,
        "estimated_seconds": round(estimated_seconds),
        "findings": findings,
    }


def build_content_card(
    cleaned_text: str,
    *,
    keyword: str,
    title: str,
    book_title: str,
    book_author: str,
    selling_points: list[str],
    settings: Settings,
    demo: bool = False,
) -> tuple[dict[str, Any], dict[str, str]]:
    user_prompt = render_prompt(
        CONTENT_CARD_USER_PROMPT,
        book_title=book_title or "待确认",
        book_author=book_author or "待确认",
        selling_points="；".join(selling_points) or "暂无已确认卖点",
        keyword=keyword,
        title=title,
        cleaned_transcript=cleaned_text,
    )
    snapshot = prompt_snapshot(
        CONTENT_CARD_PROMPT_VERSION, CONTENT_CARD_SYSTEM_PROMPT, user_prompt
    )
    if demo:
        card = {
            "core_claim": "长期积累比焦虑地追求即时结果更重要",
            "target_audience": "计划很多却难以长期坚持的读者",
            "source_facts": ["时间管理不等于把一天塞满", "成长需要长期积累"],
            "claims_needing_evidence": [],
            "product_reasons": selling_points,
            "source_phrases_to_avoid": ["真的建议大家读一读", "值得好好看看"],
            "recommended_focus": "用阅读障碍与认知反差建立购买理由",
        }
    else:
        card = _chat_json(
            settings,
            temperature=0.1,
            system=CONTENT_CARD_SYSTEM_PROMPT,
            user=user_prompt,
        )
    for key in (
        "source_facts",
        "claims_needing_evidence",
        "product_reasons",
        "source_phrases_to_avoid",
    ):
        value = card.get(key)
        card[key] = [str(item).strip() for item in value or [] if str(item).strip()]
    card.setdefault("core_claim", "围绕这本书解决的一个具体阅读问题")
    card.setdefault("target_audience", "对该主题感兴趣但尚未读懂原书的人")
    card.setdefault("recommended_focus", card["core_claim"])
    return card, snapshot


def repair_transcript(
    transcript: str,
    *,
    keyword: str,
    title: str,
    author: str,
    settings: Settings,
    output_path: Path,
    demo: bool = False,
) -> Path:
    system_prompt = REPAIR_SYSTEM_PROMPT
    user_prompt = render_prompt(
        REPAIR_USER_PROMPT,
        keyword=keyword,
        title=title,
        author=author,
        transcript=transcript,
    )
    if demo:
        cleaned = DEMO_CLEANED_TRANSCRIPT
        repairs = [
            "删除开场自我介绍和频道口号",
            "删除点赞关注等互动引导",
            "将含糊的‘这本书’补全为《把时间当作朋友》",
        ]
    else:
        cleaned = _chat_text(
            settings,
            temperature=0.1,
            system=system_prompt,
            user=user_prompt,
        )
        repairs = []
        if not cleaned:
            raise RuntimeError("逐字稿修复结果为空")
    return write_json(
        output_path,
        {
            "schema_version": 1,
            "raw_text": transcript,
            "cleaned_text": cleaned,
            "repairs": repairs,
            "findings": compliance_findings(cleaned),
            "keyword": keyword,
            "prompt": prompt_snapshot(
                REPAIR_PROMPT_VERSION, system_prompt, user_prompt
            ),
        },
    )


def rewrite_candidates(
    cleaned_text: str,
    *,
    keyword: str,
    title: str,
    author: str,
    notes: str,
    settings: Settings,
    output_path: Path,
    book_title: str = "",
    book_author: str = "",
    selling_points: list[str] | None = None,
    target_seconds: int = 60,
    rewrite_mode: str = "medium",
    demo: bool = False,
) -> Path:
    selling_points = [str(item).strip() for item in selling_points or [] if str(item).strip()]
    content_card, content_card_prompt = build_content_card(
        cleaned_text,
        keyword=keyword,
        title=title,
        book_title=book_title,
        book_author=book_author,
        selling_points=selling_points,
        settings=settings,
        demo=demo,
    )
    prompt_snapshots: list[dict[str, str]] = []
    if demo:
        variants = [
            {
                "id": "A",
                "label": "反常识冲突",
                "strategy": "contrast",
                "strategy_instruction": REWRITE_STRATEGIES[0]["instruction"],
                "script": (
                    "越想把一天安排得满满当当的人，越可能把时间耗在焦虑里。"
                    "《把时间当作朋友》真正提醒我们的，不是再买一个效率工具，而是先接受能力边界，"
                    "把注意力放回值得长期积累的事情。今天读十页、走半小时、完成一个小任务，"
                    "看起来都不惊人，却比反复换计划更接近真正的成长。如果你总想立刻看到结果，"
                    "可以从这本书开始，重新理解耐心和时间。"
                ),
            },
            {
                "id": "B",
                "label": "人群痛点",
                "strategy": "audience-pain",
                "strategy_instruction": REWRITE_STRATEGIES[1]["instruction"],
                "script": (
                    "如果你列了很多计划，却总觉得自己来不及了，先别继续逼自己。"
                    "很多时候，问题不是时间太少，而是我们把精力花在了不断选择和后悔上。"
                    "《把时间当作朋友》把这件事讲得很清楚：先判断什么值得长期做，再给它足够耐心。"
                    "它不会替你安排每一分钟，但会帮你停下对即时结果的执念。"
                    "想建立一套能坚持的节奏，可以先从每天读几页开始。"
                ),
            },
            {
                "id": "C",
                "label": "商品解决方案",
                "strategy": "product-solution",
                "strategy_instruction": REWRITE_STRATEGIES[2]["instruction"],
                "script": (
                    "很多时间管理书教你把日程排得更满，这本书却先让你接受现实。"
                    "《把时间当作朋友》讨论的不是几个立刻见效的技巧，而是时间、耐心和成长之间的关系。"
                    "它适合反复阅读，因为同一个观点放到学习、健康和工作里都会得到不同答案。"
                    "如果你已经试过很多工具，仍然很难长期坚持，不妨换一个入口："
                    "先理解时间，再决定每天究竟该做什么。"
                ),
            },
        ]
    else:
        variants = []
        for strategy in REWRITE_STRATEGIES:
            user_prompt = render_prompt(
                REWRITE_USER_PROMPT,
                strategy_label=strategy["label"],
                strategy_instruction=strategy["instruction"],
                rewrite_mode=rewrite_mode,
                target_seconds=max(15, target_seconds),
                book_title=book_title or "待确认",
                book_author=book_author or "待确认",
                selling_points="；".join(selling_points) or "暂无已确认卖点",
                keyword=keyword,
                title=title,
                author=author,
                rewrite_notes=notes,
                content_card=json_for_prompt(content_card),
                cleaned_transcript=cleaned_text,
            )
            prompt_snapshots.append(
                prompt_snapshot(REWRITE_PROMPT_VERSION, REWRITE_SYSTEM_PROMPT, user_prompt)
            )
            script = _chat_text(
                settings,
                temperature=0.7,
                system=REWRITE_SYSTEM_PROMPT,
                user=user_prompt,
            )
            variants.append(
                {
                    "id": strategy["id"],
                    "label": strategy["label"],
                    "strategy": strategy["id"],
                    "strategy_instruction": strategy["instruction"],
                    "script": script,
                }
            )
    for item in variants:
        script = str(item.get("script") or "")
        quality = score_copy_candidate(
            cleaned_text,
            script,
            book_title=book_title,
            selling_points=selling_points,
            target_seconds=max(15, target_seconds),
        )
        item["hook"] = _first_sentence(script)
        item["char_count"] = len(script)
        item["estimated_seconds"] = quality["estimated_seconds"]
        item["findings"] = quality.pop("findings")
        item["quality"] = quality
    recommended = max(variants, key=lambda item: item["quality"]["overall_score"])["id"]
    return write_json(
        output_path,
        {
            "schema_version": 2,
            "strategy": "three-distinct-strategies-with-quality-gate",
            "rewrite_mode": rewrite_mode,
            "target_seconds": target_seconds,
            "content_card": content_card,
            "content_card_prompt": content_card_prompt,
            "prompt_version": REWRITE_PROMPT_VERSION,
            "prompt_snapshots": prompt_snapshots,
            "recommended_candidate_id": recommended,
            "candidates": variants,
        },
    )


def identify_book(
    script_text: str,
    *,
    existing_title: str,
    existing_author: str,
    keyword: str,
    source_title: str,
    source_description: str,
    settings: Settings,
    output_path: Path,
    selling_points: list[str] | None = None,
    book_cover: str | None = None,
    demo: bool = False,
) -> Path:
    system_prompt = BOOK_SYSTEM_PROMPT
    user_prompt = render_prompt(
        BOOK_USER_PROMPT,
        existing_title=existing_title,
        existing_author=existing_author,
        keyword=keyword,
        source_title=source_title,
        source_description=source_description,
        script_text=script_text[:2600],
    )
    if demo:
        result = {
            "book_title": existing_title or "把时间当作朋友",
            "book_author": existing_author or "李笑来",
            "confidence": 0.98,
            "evidence": "正文中多次明确出现书名，并围绕时间、耐心和成长展开。",
        }
    else:
        result = _chat_json(
            settings,
            temperature=0.05,
            system=system_prompt,
            user=user_prompt,
        )
    result["schema_version"] = 1
    result["needs_review"] = float(result.get("confidence") or 0) < 0.6 or not result.get("book_title")
    result["selling_points"] = [
        str(item).strip() for item in selling_points or [] if str(item).strip()
    ]
    result["book_cover"] = book_cover or ""
    asset_warnings = []
    if not result["selling_points"]:
        asset_warnings.append("尚未填写已确认商品卖点，二创只能聚焦阅读价值")
    if not result["book_cover"]:
        asset_warnings.append("尚未提供真实封面，系统不会让图片模型虚构封面")
    if not result.get("book_author"):
        asset_warnings.append("作者或具体版本尚未确认")
    result["asset_warnings"] = asset_warnings
    result["product_ready"] = bool(result.get("book_title") and result["selling_points"])
    result["prompt"] = prompt_snapshot(
        BOOK_PROMPT_VERSION, system_prompt, user_prompt
    )
    return write_json(output_path, result)


def split_narration(text: str, max_chars: int = 120) -> list[str]:
    sentences = [item.strip() for item in re.split(r"(?<=[。！？；])", text) if item.strip()]
    segments: list[str] = []
    buffer = ""
    for sentence in sentences:
        if buffer and len(buffer) + len(sentence) > max_chars:
            segments.append(buffer)
            buffer = sentence
        else:
            buffer += sentence
    if buffer:
        segments.append(buffer)
    return segments or [text]


def split_narration_with_article_prompt(
    text: str,
    *,
    keyword: str,
    title: str,
    author: str,
    settings: Settings,
    demo: bool = False,
) -> tuple[list[str], dict[str, str]]:
    system_prompt = TTS_SEGMENT_SYSTEM_PROMPT
    user_prompt = render_prompt(
        TTS_SEGMENT_USER_PROMPT,
        keyword=keyword,
        title=title,
        author=author,
        script_text=text,
    )
    snapshot = prompt_snapshot(
        TTS_SEGMENT_PROMPT_VERSION, system_prompt, user_prompt
    )
    if demo:
        return split_narration(text, max_chars=120), snapshot
    result = _chat_json(
        settings,
        temperature=0.1,
        system=system_prompt,
        user=user_prompt,
    )
    raw_segments = result.get("segments")
    if not isinstance(raw_segments, list):
        raise RuntimeError("TTS 拆段结果缺少 segments 数组")
    segments = [str(item).strip() for item in raw_segments if str(item).strip()]
    if not segments:
        raise RuntimeError("TTS 拆段结果为空")
    normalize = lambda value: re.sub(r"\s+", "", value)
    if normalize("".join(segments)) != normalize(text):
        raise RuntimeError("TTS 拆段修改了原文内容，请重试")
    return segments, snapshot


def _fallback_shot(
    source: str,
    *,
    index: int,
    count: int,
    occurrence: int,
    book_title: str = "",
) -> dict[str, Any]:
    role = SHOT_ROLES[index % len(SHOT_ROLES)]
    role_details = {
        "pattern_interrupt": (
            "用强反差的具体生活瞬间建立开场冲突",
            "与主题有关的环境和一个清晰物件",
            "静态环境中出现一个打破惯性的动作",
            "近景，主体位于画面上半部，底部留字幕区",
        ),
        "establishing": (
            "建立时间、季节或阅读环境",
            "完整而有层次的真实空间",
            "光线或人物从空间中自然经过",
            "竖屏广角大景，前中后景分明",
        ),
        "human_action": (
            "把抽象观点转成可理解的日常动作",
            "固定成年读者，只露自然侧影或背影",
            "进行阅读、停顿、翻页或整理书桌等具体动作",
            "中景，人物偏侧，避免正脸特写",
        ),
        "detail": (
            "用材质和动作细节提升真实感",
            "手、纸张、光影或与主题有关的物件",
            "自然触碰、翻动或光影缓慢移动",
            "微距细节，结构完整，禁止文字和畸形手指",
        ),
        "editorial_symbol": (
            "用东方编辑意象解释抽象知识",
            "纸张、自然纹理、季节或时间意象",
            "构成元素形成清晰视觉关系，不出现文字",
            "正面编辑构图，留出大面积呼吸空间",
        ),
        "product_space": (
            "为真实图书封面或卖点卡片预留后期合成区域",
            "干净台面、纸张或阅读场景",
            "环境中形成稳定的空白展示位，不绘制具体封面",
            "中近景，右侧或中央留完整矩形安全区",
        ),
        "transition": (
            "连接前后语义并改变视觉节奏",
            "自然光、树影、纸页或空间通道",
            "光影、风或人物经过形成方向性变化",
            "竖屏纵深构图，避免与前一镜相同景别",
        ),
        "closing": (
            "形成克制、可信的结尾和行动感",
            "整洁阅读桌与真实商品后期留白区",
            "人物离开后留下一个准备继续阅读的场景",
            "稳定正面构图，标题区和字幕区均留白",
        ),
    }
    purpose, subject, action, framing = role_details[role]
    visual_brief = (
        f"全片镜头 {index + 1}/{count}，必须与其他镜头形成独立画面；"
        f"{purpose}；主体：{subject}；动作：{action}；构图：{framing}；"
        f"这是同一语义的第 {occurrence + 1} 个互补镜头，不重复前镜；"
        "宣纸米白、墨黑、少量朱砂或松石青，真实自然光，9:16 原生竖屏。"
    )
    return {
        "id": index + 1,
        "script_text": source,
        "narration": source,
        "shot_role": role,
        "visual_purpose": purpose,
        "subject": subject,
        "action": action,
        "location": "与口播主题一致的真实阅读空间或东方编辑场景",
        "framing": framing + "；底部 28% 不放关键主体",
        "lighting": "同一视频保持克制自然光与低饱和东方编辑色彩",
        "continuity": "固定同一读者、米白上衣、深色头发、同一材质体系",
        "avoid": "文字、Logo、假封面、假漫画内页、重复构图、畸形手部",
        "visual_brief": visual_brief,
        "book_title": book_title,
        "safety_translation": (
            "不要画医疗病理、身体异常、器官、伤口、病床、手术室、监护仪、"
            "注射器或惊悚画面；真实封面由后期合成。"
        ),
    }


def scene_briefs(
    script_text: str,
    count: int,
    *,
    book_title: str = "",
) -> list[dict[str, Any]]:
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[。！？；])", script_text)
        if item.strip()
    ]
    if not sentences:
        sentences = [script_text]
    occurrences: dict[int, int] = {}
    briefs = []
    for index in range(count):
        sentence_index = min(
            len(sentences) - 1,
            math.floor(index * len(sentences) / max(1, count)),
        )
        occurrence = occurrences.get(sentence_index, 0)
        occurrences[sentence_index] = occurrence + 1
        briefs.append(
            _fallback_shot(
                sentences[sentence_index],
                index=index,
                count=count,
                occurrence=occurrence,
                book_title=book_title,
            )
        )
    return briefs


def direct_scene_briefs(
    script_text: str,
    *,
    count: int,
    book_title: str,
    book_author: str,
    selling_points: list[str],
    cover_available: bool,
    settings: Settings,
    demo: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    user_prompt = render_prompt(
        SCENE_DIRECTOR_USER_PROMPT,
        count=count,
        book_title=book_title or "待确认",
        book_author=book_author or "待确认",
        selling_points="；".join(selling_points) or "暂无已确认卖点",
        cover_available="是，后期叠加" if cover_available else "否，禁止虚构",
        script_text=script_text,
    )
    snapshot = prompt_snapshot(
        SCENE_DIRECTOR_PROMPT_VERSION, SCENE_DIRECTOR_SYSTEM_PROMPT, user_prompt
    )
    fallbacks = scene_briefs(script_text, count, book_title=book_title)
    if demo:
        return fallbacks, snapshot
    result = _chat_json(
        settings,
        temperature=0.35,
        system=SCENE_DIRECTOR_SYSTEM_PROMPT,
        user=user_prompt,
    )
    raw_shots = result.get("shots")
    if not isinstance(raw_shots, list):
        raise RuntimeError("视觉导演结果缺少 shots 数组")
    briefs: list[dict[str, Any]] = []
    for index in range(count):
        fallback = fallbacks[index]
        raw = raw_shots[index] if index < len(raw_shots) and isinstance(raw_shots[index], dict) else {}
        brief = {**fallback}
        brief.update(
            {
                "id": index + 1,
                "script_text": str(raw.get("narration") or fallback["script_text"]).strip(),
                "narration": str(raw.get("narration") or fallback["narration"]).strip(),
                "shot_role": str(raw.get("shot_role") or fallback["shot_role"]).strip(),
                "visual_purpose": str(raw.get("visual_purpose") or fallback["visual_purpose"]).strip(),
                "subject": str(raw.get("subject") or fallback["subject"]).strip(),
                "action": str(raw.get("action") or fallback["action"]).strip(),
                "location": str(raw.get("location") or fallback["location"]).strip(),
                "framing": str(raw.get("framing") or fallback["framing"]).strip(),
                "lighting": str(raw.get("lighting") or fallback["lighting"]).strip(),
                "continuity": str(raw.get("continuity") or fallback["continuity"]).strip(),
                "avoid": str(raw.get("avoid") or fallback["avoid"]).strip(),
            }
        )
        brief["visual_brief"] = (
            f"全片镜头 {index + 1}/{count}，必须与其他镜头形成独立画面；"
            f"{brief['visual_purpose']}；主体：{brief['subject']}；动作：{brief['action']}；"
            f"环境：{brief['location']}；构图：{brief['framing']}；光线：{brief['lighting']}；"
            f"连续性：{brief['continuity']}；禁止：{brief['avoid']}"
        )
        briefs.append(brief)
    return briefs, snapshot

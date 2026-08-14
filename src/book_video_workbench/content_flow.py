from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from book_video_workbench.article_prompts import (
    BOOK_PROMPT_VERSION,
    BOOK_SYSTEM_PROMPT,
    BOOK_USER_PROMPT,
    REPAIR_PROMPT_VERSION,
    REPAIR_SYSTEM_PROMPT,
    REPAIR_USER_PROMPT,
    REWRITE_PROMPT_VERSION,
    REWRITE_SYSTEM_PROMPT,
    REWRITE_USER_PROMPT,
    TTS_SEGMENT_PROMPT_VERSION,
    TTS_SEGMENT_SYSTEM_PROMPT,
    TTS_SEGMENT_USER_PROMPT,
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
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = json.loads(response.read().decode("utf-8"))
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
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = json.loads(response.read().decode("utf-8"))
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
    demo: bool = False,
) -> Path:
    system_prompt = REWRITE_SYSTEM_PROMPT
    user_prompt = render_prompt(
        REWRITE_USER_PROMPT,
        keyword=keyword,
        title=title,
        author=author,
        rewrite_notes=notes,
        cleaned_transcript=cleaned_text,
    )
    if demo:
        variants = [
            {
                "id": "A",
                "label": "保留原节奏",
                "hook": "很多人以为时间管理，就是把一天塞得更满。",
                "script": cleaned_text,
            },
            {
                "id": "B",
                "label": "冲突前置",
                "hook": "越着急改变自己的人，越容易把时间浪费在焦虑上。",
                "script": cleaned_text.replace(
                    "很多人以为时间管理就是把一天塞得更满，列更多计划，买更多效率工具。",
                    "越着急改变自己的人，越容易把时间浪费在焦虑上。很多人把一天塞满，列更多计划，买更多效率工具，",
                ),
            },
            {
                "id": "C",
                "label": "人群场景",
                "hook": "如果你计划很多，却总觉得自己来不及了，先别继续逼自己。",
                "script": cleaned_text.replace(
                    "很多人以为时间管理就是把一天塞得更满，列更多计划，买更多效率工具。",
                    "如果你计划很多，却总觉得自己来不及了，先别继续逼自己。时间管理不是把一天塞得更满，",
                ),
            },
        ]
    else:
        variants = []
        for index in range(1, 4):
            script = _chat_text(
                settings,
                temperature=0.7,
                system=system_prompt,
                user=user_prompt,
            )
            first_sentence = next(
                (
                    item.strip()
                    for item in re.split(r"(?<=[。！？；])", script)
                    if item.strip()
                ),
                script[:36],
            )
            variants.append(
                {
                    "id": index,
                    "label": f"附件提示词改写版 {index}",
                    "hook": first_sentence,
                    "script": script,
                }
            )
    for item in variants:
        item["char_count"] = len(str(item.get("script") or ""))
        item["estimated_seconds"] = round(item["char_count"] / 4.2)
        item["findings"] = compliance_findings(str(item.get("script") or ""))
    return write_json(
        output_path,
        {
            "schema_version": 1,
            "strategy": "light-rewrite-preserve-viral-structure",
            "prompt": prompt_snapshot(
                REWRITE_PROMPT_VERSION, system_prompt, user_prompt
            ),
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
    result.setdefault("long_titles", [])
    result.setdefault("short_titles", [])
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


def scene_briefs(script_text: str, count: int) -> list[dict[str, Any]]:
    sentences = [item.strip() for item in re.split(r"(?<=[。！？；])", script_text) if item.strip()]
    if not sentences:
        sentences = [script_text]
    briefs = []
    for index in range(count):
        source = sentences[index % len(sentences)]
        briefs.append(
            {
                "id": index + 1,
                "script_text": source,
                "visual_brief": source,
                "safety_translation": (
                    "不要画医疗病理、身体异常、器官、伤口、病床、手术室、监护仪、"
                    "注射器或惊悚画面。"
                ),
            }
        )
    return briefs

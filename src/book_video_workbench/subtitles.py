from __future__ import annotations

import re
from pathlib import Path

from book_video_workbench.util import write_json


PUNCTUATION_RE = re.compile(r"(?<=[，。！？；：,.!?;:])")


def split_caption_text(text: str, max_chars: int = 14) -> list[str]:
    compact = re.sub(r"\s+", "", text).strip()
    if not compact:
        return []
    phrases = [part for part in PUNCTUATION_RE.split(compact) if part]
    captions: list[str] = []
    buffer = ""
    for phrase in phrases:
        while len(phrase) > max_chars:
            if buffer:
                captions.append(buffer)
                buffer = ""
            captions.append(phrase[:max_chars])
            phrase = phrase[max_chars:]
        if buffer and len(buffer) + len(phrase) > max_chars:
            captions.append(buffer)
            buffer = phrase
        else:
            buffer += phrase
    if buffer:
        captions.append(buffer)
    return captions


def proportional_timeline(text: str, duration: float) -> list[dict]:
    captions = split_caption_text(text)
    if not captions:
        raise RuntimeError("最终口播稿为空，无法生成字幕")
    weights = [max(1, len(re.sub(r"\W", "", caption))) for caption in captions]
    total = sum(weights)
    cursor = 0.0
    result: list[dict] = []
    for index, (caption, weight) in enumerate(zip(captions, weights), start=1):
        raw = duration * weight / total
        end = duration if index == len(captions) else cursor + raw
        result.append(
            {
                "id": index,
                "start": round(cursor, 3),
                "end": round(end, 3),
                "text": caption,
            }
        )
        cursor = end
    return result


def timeline_from_transcript(transcript: dict, duration: float) -> list[dict]:
    segments = transcript.get("segments") or []
    timeline = []
    for segment in segments:
        start = max(0.0, min(float(segment["start"]), duration))
        end = max(start + 0.05, min(float(segment["end"]), duration))
        captions = split_caption_text(str(segment["text"]))
        weights = [max(1, len(re.sub(r"\W", "", caption))) for caption in captions]
        total_weight = sum(weights)
        cursor = start
        for index, (caption, weight) in enumerate(zip(captions, weights), start=1):
            caption_end = (
                end
                if index == len(captions)
                else cursor + (end - start) * weight / total_weight
            )
            timeline.append(
                {
                    "id": len(timeline) + 1,
                    "start": round(cursor, 3),
                    "end": round(caption_end, 3),
                    "text": caption,
                }
            )
            cursor = caption_end
    if not timeline:
        raise RuntimeError("TTS 二次转写没有得到可用字幕")
    return timeline


def validate_timeline(timeline: list[dict], duration: float) -> None:
    previous_end = 0.0
    for item in timeline:
        start = float(item["start"])
        end = float(item["end"])
        if start < 0 or end <= start or end > duration + 0.05:
            raise RuntimeError(f"字幕时间越界: {item}")
        if start + 0.001 < previous_end:
            raise RuntimeError(f"字幕时间重叠: {item}")
        previous_end = end


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    whole_seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02},{milliseconds:03}"


def write_subtitles(
    timeline: list[dict], duration: float, json_path: Path, srt_path: Path, *, mode: str
) -> tuple[Path, Path]:
    validate_timeline(timeline, duration)
    write_json(
        json_path,
        {
            "schema_version": 1,
            "mode": mode,
            "duration_seconds": round(duration, 3),
            "items": timeline,
        },
    )
    blocks = []
    for index, item in enumerate(timeline, start=1):
        blocks.append(
            f"{index}\n{_srt_time(float(item['start']))} --> "
            f"{_srt_time(float(item['end']))}\n{item['text']}"
        )
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    srt_path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return json_path, srt_path

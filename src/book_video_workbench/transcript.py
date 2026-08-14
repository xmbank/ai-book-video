from __future__ import annotations

from pathlib import Path

import zhconv

from book_video_workbench.util import require_command, run_command, write_json


def extract_audio(video_path: Path, audio_path: Path) -> Path:
    ffmpeg = require_command("ffmpeg")
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            ffmpeg,
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(audio_path),
        ]
    )
    return audio_path


def transcribe_media(
    media_path: Path,
    output_path: Path,
    *,
    model_size: str,
    include_words: bool = True,
) -> Path:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "缺少 faster-whisper；请执行 uv sync --extra media"
        ) from exc

    model = WhisperModel(model_size, device="auto", compute_type="default")
    segments_iter, info = model.transcribe(
        str(media_path),
        language="zh",
        vad_filter=True,
        beam_size=5,
        word_timestamps=include_words,
    )
    segments: list[dict] = []
    full_text: list[str] = []
    for segment in segments_iter:
        text = zhconv.convert(segment.text.strip(), "zh-cn")
        if not text:
            continue
        words = []
        for word in segment.words or []:
            value = zhconv.convert(word.word.strip(), "zh-cn")
            if value and word.start is not None and word.end is not None:
                words.append(
                    {
                        "start": round(float(word.start), 3),
                        "end": round(float(word.end), 3),
                        "text": value,
                    }
                )
        segments.append(
            {
                "id": len(segments) + 1,
                "start": round(float(segment.start), 3),
                "end": round(float(segment.end), 3),
                "text": text,
                "words": words,
            }
        )
        full_text.append(text)
    return write_json(
        output_path,
        {
            "schema_version": 1,
            "language": info.language,
            "language_probability": info.language_probability,
            "model": model_size,
            "full_text": "\n".join(full_text),
            "segments": segments,
        },
    )

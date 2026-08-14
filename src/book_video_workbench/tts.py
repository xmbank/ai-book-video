from __future__ import annotations

import base64
import binascii
import json
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from book_video_workbench.config import Settings
from book_video_workbench.util import media_duration, require_command, run_command, write_json


def _speech_rate(speed_ratio: float) -> int:
    return max(-50, min(100, round((speed_ratio - 1.0) * 100)))


def _seed_tts_payload(text: str, voice_type: str, speed_ratio: float) -> dict:
    return {
        "req_params": {
            "text": text,
            "speaker": voice_type,
            "audio_params": {
                "format": "mp3",
                "sample_rate": 24000,
                "speech_rate": _speech_rate(speed_ratio),
            },
        }
    }


def _stream_payloads(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8", "replace")
    normalized = "\n".join(
        line[5:].lstrip() if line.lstrip().startswith("data:") else line
        for line in text.splitlines()
        if line.strip() and line.strip() != "data: [DONE]"
    )
    decoder = json.JSONDecoder()
    payloads: list[dict] = []
    offset = 0
    while offset < len(normalized):
        while offset < len(normalized) and normalized[offset].isspace():
            offset += 1
        if offset >= len(normalized):
            break
        try:
            payload, offset = decoder.raw_decode(normalized, offset)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"豆包 TTS 返回了无法解析的数据: {exc}") from exc
        if isinstance(payload, dict):
            payloads.append(payload)
    if not payloads:
        raise RuntimeError("豆包 TTS 返回为空")
    return payloads


def _decode_seed_tts_response(raw: bytes) -> tuple[bytes, dict]:
    audio = bytearray()
    result_meta: dict = {}
    for payload in _stream_payloads(raw):
        try:
            code = int(payload.get("code", 0))
        except (TypeError, ValueError):
            code = -1
        if code not in (0, 20000000):
            message = payload.get("message") or "未知错误"
            raise RuntimeError(f"豆包 TTS 请求失败 ({code}): {message}")
        encoded = payload.get("data")
        if code == 0 and encoded:
            try:
                audio.extend(base64.b64decode(encoded))
            except (binascii.Error, ValueError) as exc:
                raise RuntimeError("豆包 TTS 返回了无效的音频数据") from exc
        if payload.get("usage"):
            result_meta["usage"] = payload["usage"]
        if payload.get("message"):
            result_meta["provider_message"] = payload["message"]
    if not audio:
        raise RuntimeError("豆包 TTS 未返回音频")
    return bytes(audio), result_meta


def normalize_audio(input_path: Path, output_path: Path) -> Path:
    ffmpeg = require_command("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            ffmpeg,
            "-y",
            "-i",
            str(input_path),
            "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
    )
    return output_path


def synthesize_volcengine(
    text: str,
    output_path: Path,
    metadata_path: Path,
    settings: Settings,
    *,
    speed_ratio: float = 1.0,
) -> tuple[Path, Path]:
    required = {
        "VOLC_TTS_API_KEY": settings.volc_tts_api_key,
        "VOLC_TTS_RESOURCE_ID": settings.volc_tts_resource_id,
        "VOLC_TTS_VOICE_TYPE": settings.volc_tts_voice_type,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError("真实配音缺少配置: " + ", ".join(missing))
    request_id = str(uuid.uuid4())
    payload = _seed_tts_payload(text, settings.volc_tts_voice_type, speed_ratio)
    request = urllib.request.Request(
        settings.volc_tts_endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "X-Api-Key": settings.volc_tts_api_key,
            "X-Api-Resource-Id": settings.volc_tts_resource_id,
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            audio_bytes, response_meta = _decode_seed_tts_response(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"豆包 TTS 请求失败 ({exc.code}): {body[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"豆包 TTS 网络请求失败: {exc.reason}") from exc
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as temp:
        temp_path = Path(temp.name)
        temp.write(audio_bytes)
    try:
        normalize_audio(temp_path, output_path)
    finally:
        temp_path.unlink(missing_ok=True)
    meta = {
        "schema_version": 1,
        "provider": "volcengine-seed-tts-2.0",
        "request_id": request_id,
        "voice_type": settings.volc_tts_voice_type,
        "resource_id": settings.volc_tts_resource_id,
        "speed_ratio": speed_ratio,
        "duration_seconds": media_duration(output_path),
        **response_meta,
    }
    return output_path, write_json(metadata_path, meta)


def synthesize_macos_demo(
    text: str, output_path: Path, metadata_path: Path
) -> tuple[Path, Path]:
    say = require_command("say")
    with tempfile.TemporaryDirectory(prefix="book-video-tts-") as temp_dir:
        aiff_path = Path(temp_dir) / "speech.aiff"
        command = [say, "-v", "Tingting", "-r", "235", "-o", str(aiff_path), text]
        try:
            run_command(command, timeout=120)
        except RuntimeError:
            run_command([say, "-r", "235", "-o", str(aiff_path), text], timeout=120)
        normalize_audio(aiff_path, output_path)
    meta = {
        "schema_version": 1,
        "provider": "macos-say-demo",
        "voice": "Tingting-or-system-default",
        "duration_seconds": media_duration(output_path),
        "warning": "仅用于离线媒体链路验证，不作为正式 TTS 音质验收结果",
    }
    return output_path, write_json(metadata_path, meta)


def concatenate_audio(parts: list[Path], output_path: Path) -> Path:
    if not parts:
        raise RuntimeError("没有可拼接的 TTS 音频片段")
    ffmpeg = require_command("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="book-video-concat-") as temp_dir:
        list_path = Path(temp_dir) / "parts.txt"
        list_path.write_text(
            "\n".join(f"file '{str(path.resolve()).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'" for path in parts)
            + "\n",
            encoding="utf-8",
        )
        pending = Path(temp_dir) / "joined.wav"
        run_command(
            [
                ffmpeg,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-c:a",
                "pcm_s16le",
                str(pending),
            ]
        )
        normalize_audio(pending, output_path)
    return output_path


def synthesize_segments(
    segments: list[str],
    *,
    output_path: Path,
    metadata_path: Path,
    settings: Settings,
    demo: bool,
) -> tuple[Path, Path, list[Path]]:
    segment_dir = output_path.parent / f"{output_path.stem}-segments"
    segment_dir.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    records: list[dict] = []
    for index, text in enumerate(segments, start=1):
        audio = segment_dir / f"segment-{index:02}.wav"
        metadata = segment_dir / f"segment-{index:02}.json"
        if demo:
            synthesize_macos_demo(text, audio, metadata)
        else:
            synthesize_volcengine(text, audio, metadata, settings)
        parts.append(audio)
        records.append(
            {
                "index": index,
                "text": text,
                "audio_path": str(audio.resolve()),
                "duration_seconds": round(media_duration(audio), 3),
            }
        )
    concatenate_audio(parts, output_path)
    meta = {
        "schema_version": 2,
        "provider": "macos-say-demo" if demo else "volcengine",
        "segmented": True,
        "segment_count": len(records),
        "segments": records,
        "duration_seconds": round(media_duration(output_path), 3),
        "voice_type": settings.volc_tts_voice_type if not demo else "Tingting",
    }
    return output_path, write_json(metadata_path, meta), parts

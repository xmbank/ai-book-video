from __future__ import annotations

import json
import sys
from pathlib import Path

from book_video_workbench.config import Settings
from book_video_workbench.util import media_duration, require_command, run_command, write_json


def normalize_source_meta(meta: dict, *, duration: float, share_text: str) -> dict:
    metrics = meta.get("metrics") or {}
    return {
        "platform": "douyin",
        "external_id": meta.get("aweme_id"),
        "title": meta.get("title") or "未命名抖音视频",
        "description": meta.get("description") or meta.get("title") or "",
        "author": meta.get("author") or "未获取",
        "author_id": meta.get("author_id"),
        "source_url": meta.get("source_url") or share_text,
        "content_type": meta.get("content_type", "video"),
        "cover_url": meta.get("cover_url"),
        "duration_seconds": round(
            float(meta.get("duration_ms") or duration * 1000) / 1000, 3
        ),
        "published_at": meta.get("published_at_unix"),
        "metrics": {
            key: {
                "value": metrics.get(key),
                "reason": None if metrics.get(key) is not None else "平台未返回",
            }
            for key in ("play", "like", "comment", "collect", "share")
        },
        "download_url": meta.get("download_url") or "",
    }


def capture_douyin(
    share_text: str,
    task_dir: Path,
    settings: Settings,
) -> tuple[Path, Path, Path]:
    backend_main = settings.capture_backend_dir / "main.py"
    if not backend_main.is_file():
        raise RuntimeError(f"未找到现有抖音采集后端: {backend_main}")

    source_dir = task_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    proc = run_command(
        [
            sys.executable,
            "-m",
            "book_video_workbench.douyin_bridge",
            str(settings.capture_backend_dir),
            share_text,
        ],
        log_path=task_dir / "logs" / "source-resolve.log",
        timeout=240,
    )
    meta = None
    for line in reversed(proc.stdout.splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("aweme_id"):
            meta = candidate
            break
    if not meta:
        raise RuntimeError("现有抖音解析器未返回结构化元数据")
    raw_meta_path = write_json(source_dir / "meta.raw.json", meta)
    if meta.get("content_type") != "video":
        raise RuntimeError("P0 真实链路当前只支持抖音视频作品")
    download_url = str(meta.get("download_url") or "")
    if not download_url:
        raise RuntimeError("抖音解析成功，但没有获得视频下载地址")
    video_path = source_dir / "video.mp4"
    curl = require_command("curl")
    run_command(
        [
            curl,
            "--fail",
            "--location",
            "--retry",
            "3",
            "--retry-all-errors",
            "--connect-timeout",
            "20",
            "--max-time",
            "300",
            "--user-agent",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
            "--referer",
            "https://www.iesdouyin.com/",
            "--output",
            str(video_path),
            download_url,
        ],
        log_path=task_dir / "logs" / "source-download.log",
        timeout=330,
    )
    duration = media_duration(video_path)
    normalized = normalize_source_meta(meta, duration=duration, share_text=share_text)
    normalized_path = write_json(task_dir / "source" / "meta.normalized.json", normalized)
    return raw_meta_path, normalized_path, video_path

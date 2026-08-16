from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


GENERIC_PUBLIC_ERROR = "本步骤运行失败，详细技术信息已保存在任务日志中。请检查输入或服务设置后重试。"


def public_error_message(error: Exception | str) -> tuple[str, bool]:
    """Return a short user-facing message while keeping diagnostics off the API."""
    text = str(error).strip()
    lowered = text.lower()

    if "image_generation_transient_error" in lowered:
        return (
            "图片生成服务连接暂时中断，系统已自动重试。已生成的竖屏图会保留，请稍后点击“重试”继续。",
            True,
        )

    network_markers = (
        "douyin_share_fetch_failed",
        "sslerror",
        "unexpected_eof_while_reading",
        "connectionerror",
        "connection reset",
        "connection aborted",
        "connecttimeout",
        "readtimeout",
        "timed out",
        "max retries exceeded",
    )
    if any(marker in lowered for marker in network_markers):
        return "抖音短链连接被临时中断，系统已自动重试。请稍后点击“重试”。", True
    if "未在输入中找到抖音链接" in text:
        return "没有识别到抖音链接，请粘贴完整分享文案或有效的抖音网址。", False
    if "只支持抖音视频作品" in text or "content_type" in lowered and "image" in lowered:
        return "已识别为抖音图文作品。当前版本仅支持视频作品。", False
    if "没有获得视频下载地址" in text or "未找到视频播放地址" in text:
        return "抖音作品已解析，但暂未获得视频下载地址，请稍后重试。", True
    if "douyin_detail_cookie_unavailable" in lowered:
        return "抖音分享页未返回公开数据，且未能读取 Chrome 中的抖音登录状态。请先在 Chrome 登录抖音后重试。", True
    if "douyin_detail_api_empty" in lowered or "douyin_detail_fallback_failed" in lowered:
        return "抖音分享页未返回公开数据，登录会话也未能读取该作品。请确认 Chrome 已登录抖音后重试。", True
    if "分享页未找到作品数据" in text or "未返回结构化元数据" in text:
        return "未能从抖音分享页读取作品数据，链接可能已失效或访问受限。", True
    if "未找到现有抖音采集后端" in text:
        return "抖音采集组件未正确安装，请检查本机服务配置。", False
    if "命令失败" in text or "traceback (most recent call last)" in lowered:
        return GENERIC_PUBLIC_ERROR, False

    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    unsafe_markers = ("/users/", "site-packages", "python -m", "python3", "http://", "https://")
    if not first_line or len(first_line) > 180 or any(item in first_line.lower() for item in unsafe_markers):
        return GENERIC_PUBLIC_ERROR, False
    return first_line, False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    pending.replace(path)
    return path


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_command(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise RuntimeError(f"缺少命令: {name}")
    return resolved


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    log_path: Path | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(proc.stdout or "", encoding="utf-8")
    if proc.returncode != 0:
        output = (proc.stdout or "").strip()
        tail = "\n".join(output.splitlines()[-30:])
        raise RuntimeError(
            f"命令失败 ({proc.returncode}): {' '.join(command)}"
            + (f"\n{tail}" if tail else "")
        )
    return proc


def media_duration(path: Path) -> float:
    ffprobe = require_command("ffprobe")
    proc = run_command(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    return float(proc.stdout.strip())

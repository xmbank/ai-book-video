from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys

from book_video_workbench.config import Settings


def _version(command: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.splitlines()[0] if proc.returncode == 0 and proc.stdout else None


def diagnose(settings: Settings) -> dict:
    hyperframes = settings.project_root / "node_modules" / ".bin" / "hyperframes"
    checks = {
        "python": {
            "ok": sys.version_info >= (3, 11) and sys.version_info < (3, 14),
            "value": sys.version.split()[0],
        },
        "node": {"ok": bool(shutil.which("node")), "value": _version(["node", "--version"])},
        "ffmpeg": {
            "ok": bool(shutil.which("ffmpeg")),
            "value": _version(["ffmpeg", "-version"]),
        },
        "ffprobe": {"ok": bool(shutil.which("ffprobe")), "value": shutil.which("ffprobe")},
        "hyperframes": {
            "ok": hyperframes.is_file(),
            "value": _version([str(hyperframes), "--version"]) if hyperframes.is_file() else None,
        },
        "capture_backend": {
            "ok": (settings.capture_backend_dir / "main.py").is_file(),
            "value": str(settings.capture_backend_dir),
        },
        "faster_whisper": {
            "ok": importlib.util.find_spec("faster_whisper") is not None,
            "value": "installed" if importlib.util.find_spec("faster_whisper") else None,
        },
        "llm_credentials": {
            "ok": bool(settings.llm_api_key and settings.llm_model),
            "value": settings.llm_model or None,
        },
        "image_credentials": {
            "ok": bool(settings.image_api_key and settings.image_model),
            "value": settings.image_model or None,
        },
        "volc_tts_credentials": {
            "ok": bool(
                settings.volc_tts_api_key
                and settings.volc_tts_resource_id
                and settings.volc_tts_voice_type
            ),
            "value": (
                f"{settings.volc_tts_resource_id} / {settings.volc_tts_voice_type}"
                if settings.volc_tts_resource_id and settings.volc_tts_voice_type
                else None
            ),
        },
    }
    return {
        "ready_for_offline_demo": all(
            checks[name]["ok"] for name in ["python", "node", "ffmpeg", "ffprobe", "hyperframes"]
        ),
        "ready_for_real_pipeline": all(item["ok"] for item in checks.values()),
        "checks": checks,
    }


def format_diagnosis(report: dict) -> str:
    lines = []
    for name, item in report["checks"].items():
        mark = "OK" if item["ok"] else "MISSING"
        value = f" ({item['value']})" if item.get("value") else ""
        lines.append(f"{mark:7} {name}{value}")
    lines.append("")
    lines.append(f"离线样片: {'可运行' if report['ready_for_offline_demo'] else '未就绪'}")
    lines.append(f"真实链路: {'可运行' if report['ready_for_real_pipeline'] else '仍缺配置'}")
    return "\n".join(lines)

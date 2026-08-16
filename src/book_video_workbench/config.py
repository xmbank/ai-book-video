from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CAPTURE_BACKEND = Path(
    "/Users/qiulianglong/Documents/cc的文档/自媒体内容工厂/_系统/tools/"
    "obsidian-content-capture-backend"
)


def load_dotenv(path: Path | None = None) -> None:
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _path_from_env(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    if not value:
        return default.resolve()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


@dataclass(frozen=True)
class Settings:
    project_root: Path
    data_dir: Path
    capture_backend_dir: Path
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    image_base_url: str
    image_api_key: str
    image_model: str
    image_size: str
    volc_tts_endpoint: str
    volc_tts_api_key: str
    volc_tts_resource_id: str
    volc_tts_voice_type: str
    reference_demo_video: Path

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            project_root=PROJECT_ROOT,
            data_dir=_path_from_env("WORKBENCH_DATA_DIR", PROJECT_ROOT / "data"),
            capture_backend_dir=_path_from_env(
                "CAPTURE_BACKEND_DIR", DEFAULT_CAPTURE_BACKEND
            ),
            llm_base_url=os.environ.get(
                "LLM_BASE_URL", "https://api.openai.com/v1"
            ).rstrip("/"),
            llm_api_key=os.environ.get("LLM_API_KEY", ""),
            llm_model=os.environ.get("LLM_MODEL", ""),
            image_base_url=os.environ.get(
                "IMAGE_BASE_URL",
                os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
            ).rstrip("/"),
            image_api_key=os.environ.get(
                "IMAGE_API_KEY", os.environ.get("LLM_API_KEY", "")
            ),
            image_model=os.environ.get("IMAGE_MODEL", "gpt-image-2"),
            image_size=os.environ.get("IMAGE_SIZE", "1024x1536"),
            volc_tts_endpoint=os.environ.get(
                "VOLC_TTS_ENDPOINT",
                "https://openspeech.bytedance.com/api/v3/tts/unidirectional",
            ),
            volc_tts_api_key=os.environ.get(
                "VOLC_TTS_API_KEY",
                os.environ.get("VOLC_TTS_ACCESS_TOKEN", ""),
            ),
            volc_tts_resource_id=os.environ.get(
                "VOLC_TTS_RESOURCE_ID", "seed-tts-2.0"
            ),
            volc_tts_voice_type=os.environ.get(
                "VOLC_TTS_VOICE_TYPE", "zh_female_vv_uranus_bigtts"
            ),
            reference_demo_video=_path_from_env(
                "REFERENCE_DEMO_VIDEO",
                Path(
                    "/Users/qiulianglong/Documents/cc的文档/01 AI+产业/00 项目机会/"
                    "assets/video-01-成片演示.mp4"
                ),
            ),
        )

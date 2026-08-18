from __future__ import annotations

import base64
import binascii
import json
import os
import threading
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field

from book_video_workbench.config import Settings
from book_video_workbench.content_flow import product_asset_warnings, product_is_ready
from book_video_workbench.doctor import diagnose
from book_video_workbench.pipeline import (
    Pipeline,
    RunOptions,
    create_task,
    primary_visual_style,
)
from book_video_workbench.scene_images import generate_ai_book_cover
from book_video_workbench.state import PipelineState, STAGES
from book_video_workbench.subtitles import validate_timeline, write_subtitles
from book_video_workbench.util import (
    media_duration,
    public_error_message,
    read_json,
    utc_now,
    write_json,
)


settings = Settings.from_env()
app = FastAPI(title="AI 图书带货视频工作台", version="0.1.0")
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="book-video-worker")
running_tasks: dict[str, Any] = {}
running_lock = threading.Lock()


class TaskCreate(BaseModel):
    mode: Literal["real", "demo"] = "real"
    share_text: str = ""
    book_title: str = ""
    book_author: str = ""
    selling_points: list[str] = Field(default_factory=list)
    target_seconds: int = Field(default=90, ge=10, le=1200)
    whisper_model: str = "small"
    subtitle_mode: Literal["whisper", "proportional"] = "whisper"
    book_cover: str | None = None
    allow_source_video: bool = False
    keyword: str = "图书带货"
    rewrite_mode: Literal["light", "medium", "deep"] = "medium"
    rewrite_notes: str = "保留可核实事实，重组叙事角度、信息顺序和表达，避免同义词式洗稿"
    scene_count: int = Field(default=0, ge=0, le=63)
    styles: list[str] = Field(default_factory=lambda: ["book-sales"])
    style_counts: dict[str, int] = Field(default_factory=lambda: {"book-sales": 1})
    declaration: str = "本视频基于公开资料整理，仅作阅读分享，不构成医疗建议或行为指导。"


class ScriptUpdate(BaseModel):
    script: str = Field(min_length=1)


class RepairUpdate(BaseModel):
    cleaned_text: str = Field(min_length=1)


class BookUpdate(BaseModel):
    book_title: str = Field(min_length=1)
    book_author: str = ""
    confidence: float = Field(default=1.0, ge=0, le=1)
    selling_points: list[str] = Field(default_factory=list)
    book_cover: str | None = None
    rewrite_mode: Literal["light", "medium", "deep"] = "medium"
    target_seconds: int | None = Field(default=None, ge=10, le=1200)


class BookCoverUpload(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    data_url: str = Field(min_length=32)


class BookCoverGenerate(BaseModel):
    book_title: str = Field(min_length=1)
    book_author: str = ""
    selling_points: list[str] = Field(default_factory=list)


class StyleUpdate(BaseModel):
    styles: list[str]
    style_counts: dict[str, int] = Field(default_factory=dict)
    declaration: str
    scene_count: int = Field(ge=0, le=63)


class SubtitleItem(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = Field(min_length=1)


class SubtitleUpdate(BaseModel):
    items: list[SubtitleItem]


class SettingsUpdate(BaseModel):
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    image_base_url: str | None = None
    image_api_key: str | None = None
    image_model: str | None = None
    image_size: str | None = None
    volc_tts_endpoint: str | None = None
    volc_tts_api_key: str | None = None
    volc_tts_resource_id: str | None = None
    volc_tts_voice_type: str | None = None


def _task_dir(task_id: str) -> Path:
    if "/" in task_id or ".." in task_id:
        raise HTTPException(404, "任务不存在")
    path = (settings.data_dir / "tasks" / task_id).resolve()
    root = (settings.data_dir / "tasks").resolve()
    if path.parent != root or not (path / "task.json").is_file():
        raise HTTPException(404, "任务不存在")
    return path


def _path_for(task_dir: Path, relative: str | None) -> Path | None:
    if not relative:
        return None
    target = (task_dir / relative).resolve()
    if task_dir not in target.parents or not target.is_file():
        return None
    return target


def _artifact_value(task_dir: Path, task: dict, key: str) -> Any:
    target = _path_for(task_dir, (task.get("active_artifacts") or {}).get(key))
    if not target:
        return None
    if target.suffix == ".json":
        return read_json(target)
    return {"path": str(target.relative_to(task_dir)), "size_bytes": target.stat().st_size}


def _overall_status(state: dict) -> str:
    statuses = [item["status"] for item in state["stages"].values()]
    if any(value == "running" for value in statuses):
        return "processing"
    if any(value == "failed" for value in statuses):
        return "failed"
    if state["stages"].get("review", {}).get("status") == "succeeded":
        return "rendered"
    if any(value == "stale" for value in statuses):
        return "outdated"
    return "draft"


def _public_stages(stages: dict) -> dict:
    """Remove stored diagnostics from API responses, including legacy failures."""
    public: dict[str, dict] = {}
    for name, record in stages.items():
        cleaned = {key: value for key, value in record.items() if key != "error"}
        error = record.get("error")
        if record.get("status") == "failed":
            error_record = error if isinstance(error, dict) else {}
            diagnostic = "\n".join(
                str(value)
                for value in (
                    error_record.get("technical_message"),
                    error_record.get("message"),
                    error_record.get("traceback"),
                    record.get("message"),
                )
                if value
            )
            message, retryable = public_error_message(diagnostic, stage=name)
            cleaned["message"] = message
            cleaned["error"] = {
                "type": error_record.get("type") or "Error",
                "message": message,
                "retryable": retryable,
            }
        elif error is None:
            cleaned["error"] = None
        public[name] = cleaned
    return public


def _task_summary(task_dir: Path, *, detail: bool = False) -> dict:
    task = read_json(task_dir / "task.json")
    state_model = PipelineState(task_dir)
    state = state_model.data
    meta = read_json(task_dir / "source" / "meta.normalized.json") if (task_dir / "source" / "meta.normalized.json").is_file() else {}
    active = task.get("active_artifacts") or {}
    final_video = _path_for(task_dir, active.get("final_video"))
    result = {
        "id": task["id"],
        "created_at": task["created_at"],
        "updated_at": task.get("updated_at") or state.get("updated_at"),
        "title": meta.get("title") or task["options"].get("book_title") or "处理中",
        "author": meta.get("author") or "未获取",
        "book_title": (_artifact_value(task_dir, task, "book_info") or {}).get("book_title") or task["options"].get("book_title"),
        "mode": task["options"].get("mode"),
        "overall_status": _overall_status(state),
        "stages": _public_stages(state["stages"]),
        "has_video": bool(final_video),
        "video_url": f"/api/tasks/{task['id']}/media/{active.get('final_video')}" if final_video else None,
        "metrics": meta.get("metrics") or {},
        "duration_seconds": meta.get("duration_seconds"),
    }
    if detail:
        scene_manifest = _artifact_value(task_dir, task, "scene_manifest")
        if scene_manifest:
            for key in ("grids", "contact_sheets", "scenes"):
                urls = []
                for value in scene_manifest.get(key) or []:
                    path = Path(value)
                    try:
                        relative = path.resolve().relative_to(task_dir)
                    except ValueError:
                        continue
                    urls.append(f"/api/tasks/{task['id']}/media/{relative}")
                scene_manifest[f"{key}_urls"] = urls
        output_index = _artifact_value(task_dir, task, "output_index")
        if output_index:
            for item in output_index.get("outputs") or []:
                path = Path(item.get("video_path") or "")
                try:
                    relative = path.resolve().relative_to(task_dir)
                except ValueError:
                    continue
                item["video_url"] = f"/api/tasks/{task['id']}/media/{relative}"
        result.update(
            {
                "options": task["options"],
                "meta": meta,
                "active_artifacts": active,
                "raw_transcript": _artifact_value(task_dir, task, "raw_transcript") or _artifact_value(task_dir, task, "transcript"),
                "repaired_transcript": _artifact_value(task_dir, task, "repaired_transcript"),
                "rewrite_candidates": _artifact_value(task_dir, task, "rewrite_candidates"),
                "selected_script": _artifact_value(task_dir, task, "selected_script"),
                "tts_plan": _artifact_value(task_dir, task, "tts_plan"),
                "tts_metadata": _artifact_value(task_dir, task, "tts_metadata"),
                "subtitles": _artifact_value(task_dir, task, "subtitle_json"),
                "scene_manifest": scene_manifest,
                "book_info": _artifact_value(task_dir, task, "book_info"),
                "style_config": _artifact_value(task_dir, task, "style_config"),
                "output_index": output_index,
                "review_report": _artifact_value(task_dir, task, "review_report"),
                "audio_url": f"/api/tasks/{task['id']}/media/{active.get('tts_audio')}" if _path_for(task_dir, active.get("tts_audio")) else None,
                "source_video_url": f"/api/tasks/{task['id']}/media/source/video.mp4" if (task_dir / "source" / "video.mp4").is_file() else None,
            }
        )
    return result


def _submit(task_dir: Path, force_stage: str | None = None) -> None:
    task_id = task_dir.name
    with running_lock:
        current = running_tasks.get(task_id)
        if current and not current.done():
            raise HTTPException(409, "任务正在运行")
        running_tasks[task_id] = executor.submit(
            Pipeline(task_dir, Settings.from_env()).run, force_stage
        )


def _next_version(task_dir: Path, directory: str, prefix: str, suffix: str) -> Path:
    folder = task_dir / directory
    folder.mkdir(parents=True, exist_ok=True)
    versions = []
    for candidate in folder.glob(f"{prefix}*{suffix}"):
        middle = candidate.name[len(prefix) : -len(suffix)]
        if middle.isdigit():
            versions.append(int(middle))
    return folder / f"{prefix}{max(versions, default=0) + 1}{suffix}"


def _activate(task_dir: Path, key: str, path: Path) -> None:
    task_path = task_dir / "task.json"
    task = read_json(task_path)
    task.setdefault("active_artifacts", {})[key] = str(path.relative_to(task_dir))
    task["updated_at"] = utc_now()
    write_json(task_path, task)


def _record_manual(task_dir: Path, stage: str, paths: list[Path]) -> None:
    state = PipelineState(task_dir)
    state.data["stages"][stage].update(
        {"status": "succeeded", "message": "已保存新版本", "ended_at": utc_now()}
    )
    state._record_artifacts(stage, paths)
    state.save()


@app.get("/api/health")
def health() -> dict:
    return {"service": "ai-book-video-workbench", **diagnose(Settings.from_env())}


@app.get("/api/tasks")
def list_tasks() -> list[dict]:
    root = settings.data_dir / "tasks"
    root.mkdir(parents=True, exist_ok=True)
    tasks = [
        _task_summary(path)
        for path in root.iterdir()
        if path.is_dir() and (path / "task.json").is_file()
    ]
    return sorted(tasks, key=lambda item: item["created_at"], reverse=True)


@app.post("/api/tasks", status_code=202)
def create_web_task(value: TaskCreate) -> dict:
    if value.mode == "real" and not value.share_text.strip():
        raise HTTPException(422, "真实任务需要抖音分享链接")
    if value.scene_count != 0 and not 6 <= value.scene_count <= 63:
        raise HTTPException(422, "场景图数量必须为自动，或在 6 到 63 之间")
    task_dir = create_task(settings, RunOptions(**value.model_dump()))
    _submit(task_dir)
    return _task_summary(task_dir, detail=True)


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    return _task_summary(_task_dir(task_id), detail=True)


@app.post("/api/tasks/{task_id}/stages/{stage}/run", status_code=202)
def run_stage(task_id: str, stage: str) -> dict:
    if stage not in STAGES:
        raise HTTPException(404, "阶段不存在")
    task_dir = _task_dir(task_id)
    _submit(task_dir, stage)
    return {"accepted": True, "task_id": task_id, "stage": stage}


@app.patch("/api/tasks/{task_id}/script")
def update_script(task_id: str, value: ScriptUpdate) -> dict:
    task_dir = _task_dir(task_id)
    task = read_json(task_dir / "task.json")
    previous = _artifact_value(task_dir, task, "selected_script") or {}
    output = _next_version(task_dir, "scripts", "selected-v", ".json")
    write_json(
        output,
        {
            **previous,
            "schema_version": 1,
            "script": value.script.strip(),
            "edited_at": utc_now(),
            "edited_in": "web",
        },
    )
    _activate(task_dir, "selected_script", output)
    _record_manual(task_dir, "rewrite", [output])
    PipelineState(task_dir).invalidate_from("audio")
    return _task_summary(task_dir, detail=True)


@app.patch("/api/tasks/{task_id}/repair")
def update_repair(task_id: str, value: RepairUpdate) -> dict:
    task_dir = _task_dir(task_id)
    task = read_json(task_dir / "task.json")
    previous = _artifact_value(task_dir, task, "repaired_transcript") or {}
    output = _next_version(task_dir, "transcript", "repaired-v", ".json")
    write_json(
        output,
        {
            **previous,
            "schema_version": 1,
            "cleaned_text": value.cleaned_text.strip(),
            "edited_at": utc_now(),
            "edited_in": "web",
        },
    )
    _activate(task_dir, "repaired_transcript", output)
    _record_manual(task_dir, "repair", [output])
    PipelineState(task_dir).invalidate_from("book_info")
    return _task_summary(task_dir, detail=True)


@app.patch("/api/tasks/{task_id}/book")
def update_book(task_id: str, value: BookUpdate) -> dict:
    task_dir = _task_dir(task_id)
    current_task = read_json(task_dir / "task.json")
    previous_book = _artifact_value(task_dir, current_task, "book_info") or {}
    suggested_selling_points = [
        str(item).strip()
        for item in previous_book.get("suggested_selling_points") or []
        if str(item).strip()
    ]
    provided_selling_points = [item.strip() for item in value.selling_points if item.strip()]
    selling_points = provided_selling_points or suggested_selling_points[:3]
    cover_value = (value.book_cover or "").strip()
    if cover_value:
        cover = Path(cover_value).expanduser().resolve()
        if not cover.is_file():
            raise HTTPException(422, "封面图片路径不存在")
        cover_value = str(cover)
    output = _next_version(task_dir, "book", "identity-v", ".json")
    write_json(
        output,
        {
            "schema_version": 1,
            "book_title": value.book_title.strip(),
            "book_author": value.book_author.strip(),
            "confidence": value.confidence,
            "evidence": "用户在工作台确认",
            "needs_review": False,
            "suggested_selling_points": suggested_selling_points,
            "selling_points": selling_points,
            "selling_points_source": (
                "user_provided" if provided_selling_points else "ai_extracted_from_source"
            ),
            "book_cover": cover_value,
            "book_cover_source": (
                "ai_generated"
                if cover_value and Path(cover_value).name.startswith("ai-cover-v")
                else "user_provided" if cover_value else ""
            ),
            "asset_warnings": product_asset_warnings(
                book_author=value.book_author.strip(),
                selling_points=selling_points,
                book_cover=cover_value,
            ),
            "product_ready": product_is_ready(
                book_title=value.book_title.strip(),
                selling_points=selling_points,
                book_cover=cover_value,
            ),
            "confirmed_at": utc_now(),
        },
    )
    task_path = task_dir / "task.json"
    task = read_json(task_path)
    task["options"]["book_title"] = value.book_title.strip()
    task["options"]["book_author"] = value.book_author.strip()
    task["options"]["selling_points"] = selling_points
    task["options"]["book_cover"] = cover_value or None
    task["options"]["rewrite_mode"] = value.rewrite_mode
    if value.target_seconds is not None:
        task["options"]["target_seconds"] = value.target_seconds
    task["updated_at"] = utc_now()
    write_json(task_path, task)
    _activate(task_dir, "book_info", output)
    _record_manual(task_dir, "book_info", [output])
    PipelineState(task_dir).invalidate_from("rewrite")
    return _task_summary(task_dir, detail=True)


@app.post("/api/tasks/{task_id}/book-cover/generate")
def generate_book_cover(task_id: str, value: BookCoverGenerate) -> dict:
    task_dir = _task_dir(task_id)
    output = _next_version(task_dir, "book/assets", "ai-cover-v", ".jpg")
    metadata = output.with_suffix(".json")
    try:
        generate_ai_book_cover(
            book_title=value.book_title,
            book_author=value.book_author,
            selling_points=value.selling_points,
            output_path=output,
            metadata_path=metadata,
            settings=Settings.from_env(),
        )
    except RuntimeError as exc:
        message, _ = public_error_message(exc)
        raise HTTPException(502, message) from exc
    return {
        "path": str(output.resolve()),
        "media_url": f"/api/tasks/{task_id}/media/{output.relative_to(task_dir)}",
        "source": "ai_generated",
        "metadata_path": str(metadata.resolve()),
    }


@app.post("/api/tasks/{task_id}/book-cover")
def upload_book_cover(task_id: str, value: BookCoverUpload) -> dict:
    task_dir = _task_dir(task_id)
    header, separator, encoded = value.data_url.partition(",")
    mime_to_format = {
        "data:image/jpeg;base64": ("JPEG", ".jpg"),
        "data:image/png;base64": ("PNG", ".png"),
        "data:image/webp;base64": ("WEBP", ".webp"),
    }
    requested = mime_to_format.get(header.lower())
    if separator != "," or not requested:
        raise HTTPException(422, "封面仅支持 JPG、PNG 或 WebP 图片")
    if len(encoded) > 14_000_000:
        raise HTTPException(422, "封面图片不能超过 10 MB")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(422, "封面图片数据无效") from exc
    if not raw or len(raw) > 10 * 1024 * 1024:
        raise HTTPException(422, "封面图片不能超过 10 MB")
    try:
        with Image.open(BytesIO(raw)) as source:
            source.verify()
        with Image.open(BytesIO(raw)) as source:
            if (source.format or "").upper() != requested[0]:
                raise HTTPException(422, "封面文件类型与图片内容不一致")
            image = ImageOps.exif_transpose(source)
            image.load()
            if image.width < 200 or image.height < 200:
                raise HTTPException(422, "封面图片分辨率过低，短边至少 200 像素")
            image.thumbnail((2400, 3200), Image.Resampling.LANCZOS)
            if requested[0] == "JPEG" and image.mode not in {"RGB", "L"}:
                background = Image.new("RGB", image.size, "white")
                if "A" in image.getbands():
                    background.paste(image, mask=image.getchannel("A"))
                else:
                    background.paste(image.convert("RGB"))
                image = background
            assets_dir = task_dir / "book" / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            output = assets_dir / f"cover{requested[1]}"
            temporary = assets_dir / f"cover-upload{requested[1]}"
            save_options = {"quality": 94} if requested[0] in {"JPEG", "WEBP"} else {"optimize": True}
            image.save(temporary, format=requested[0], **save_options)
            temporary.replace(output)
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(422, "无法读取封面图片，请换一张有效图片") from exc
    return {
        "filename": value.filename,
        "path": str(output.resolve()),
        "media_url": f"/api/tasks/{task_id}/media/{output.relative_to(task_dir)}",
    }


@app.patch("/api/tasks/{task_id}/styles")
def update_styles(task_id: str, value: StyleUpdate) -> dict:
    task_dir = _task_dir(task_id)
    task_path = task_dir / "task.json"
    task = read_json(task_path)
    previous_styles = list(task["options"].get("styles") or [])
    previous_counts = dict(task["options"].get("style_counts") or {})
    previous_visual_style = primary_visual_style(previous_styles, previous_counts)
    previous_scene_count_value = task["options"].get("scene_count")
    previous_scene_count = int(
        previous_scene_count_value if previous_scene_count_value is not None else 18
    )
    task["options"]["styles"] = value.styles
    task["options"]["style_counts"] = value.style_counts
    task["options"]["declaration"] = value.declaration
    task["options"]["scene_count"] = value.scene_count
    task["updated_at"] = utc_now()
    write_json(task_path, task)
    state = PipelineState(task_dir)
    next_visual_style = primary_visual_style(value.styles, value.style_counts)
    state.invalidate_from(
        "scene_images"
        if previous_scene_count != value.scene_count
        or previous_visual_style != next_visual_style
        else "styles"
    )
    return _task_summary(task_dir, detail=True)


@app.patch("/api/tasks/{task_id}/subtitles")
def update_subtitles(task_id: str, value: SubtitleUpdate) -> dict:
    task_dir = _task_dir(task_id)
    task = read_json(task_dir / "task.json")
    audio = _path_for(task_dir, (task.get("active_artifacts") or {}).get("tts_audio"))
    if not audio:
        raise HTTPException(409, "尚无可用 TTS 音频")
    duration = media_duration(audio)
    items = [item.model_dump() | {"id": index} for index, item in enumerate(value.items, 1)]
    try:
        validate_timeline(items, duration)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc
    output = _next_version(task_dir, "subtitles", "v", ".json")
    srt = output.with_suffix(".srt")
    write_subtitles(items, duration, output, srt, mode="web-edited")
    _activate(task_dir, "subtitle_json", output)
    _activate(task_dir, "subtitle_srt", srt)
    _record_manual(task_dir, "audio", [output, srt])
    PipelineState(task_dir).invalidate_from("scene_images")
    return _task_summary(task_dir, detail=True)


@app.get("/api/tasks/{task_id}/media/{relative_path:path}")
def task_media(task_id: str, relative_path: str) -> FileResponse:
    task_dir = _task_dir(task_id)
    target = _path_for(task_dir, relative_path)
    if not target:
        raise HTTPException(404, "文件不存在")
    return FileResponse(target)


@app.get("/api/settings")
def get_settings() -> dict:
    current = Settings.from_env()
    return {
        "llm_base_url": current.llm_base_url,
        "llm_model": current.llm_model,
        "llm_api_key_configured": bool(current.llm_api_key),
        "image_base_url": current.image_base_url,
        "image_model": current.image_model,
        "image_size": current.image_size,
        "image_api_key_configured": bool(current.image_api_key),
        "volc_tts_endpoint": current.volc_tts_endpoint,
        "volc_tts_resource_id": current.volc_tts_resource_id,
        "volc_tts_voice_type": current.volc_tts_voice_type,
        "volc_tts_api_key_configured": bool(current.volc_tts_api_key),
    }


@app.patch("/api/settings")
def update_settings(value: SettingsUpdate) -> dict:
    global settings
    mapping = {
        "llm_base_url": "LLM_BASE_URL",
        "llm_api_key": "LLM_API_KEY",
        "llm_model": "LLM_MODEL",
        "image_base_url": "IMAGE_BASE_URL",
        "image_api_key": "IMAGE_API_KEY",
        "image_model": "IMAGE_MODEL",
        "image_size": "IMAGE_SIZE",
        "volc_tts_endpoint": "VOLC_TTS_ENDPOINT",
        "volc_tts_api_key": "VOLC_TTS_API_KEY",
        "volc_tts_resource_id": "VOLC_TTS_RESOURCE_ID",
        "volc_tts_voice_type": "VOLC_TTS_VOICE_TYPE",
    }
    env_path = settings.project_root / ".env"
    existing: dict[str, str] = {}
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, raw = line.split("=", 1)
                existing[key.strip()] = raw.strip()
    for field, env_name in mapping.items():
        content = getattr(value, field)
        if content is not None and content != "":
            existing[env_name] = content.replace("\n", "").replace("\r", "")
            os.environ[env_name] = existing[env_name]
    env_path.write_text("\n".join(f"{key}={raw}" for key, raw in existing.items()) + "\n", encoding="utf-8")
    settings = Settings.from_env()
    return get_settings()


dist_dir = settings.project_root / "dist"
if dist_dir.is_dir():
    app.mount("/", StaticFiles(directory=dist_dir, html=True), name="frontend")


def main() -> None:
    uvicorn.run(
        "book_video_workbench.web:app",
        host=os.environ.get("WORKBENCH_HOST", "127.0.0.1"),
        port=int(os.environ.get("WORKBENCH_PORT", "8765")),
        reload=False,
    )


if __name__ == "__main__":
    main()

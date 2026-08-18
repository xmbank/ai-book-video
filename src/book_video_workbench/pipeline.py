from __future__ import annotations

import math
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from book_video_workbench.config import Settings
from book_video_workbench.content_flow import (
    DEMO_RAW_TRANSCRIPT,
    identify_book,
    product_asset_warnings,
    product_is_ready,
    repair_transcript,
    rewrite_candidates,
    split_narration_with_article_prompt,
)
from book_video_workbench.renderer import generate_project, render_project, validate_video
from book_video_workbench.scene_images import generate_ai_book_cover, generate_scene_images
from book_video_workbench.source import capture_douyin
from book_video_workbench.state import PipelineState, STAGES
from book_video_workbench.storyboard import build_storyboard
from book_video_workbench.subtitles import proportional_timeline, write_subtitles
from book_video_workbench.transcript import extract_audio, transcribe_media
from book_video_workbench.tts import synthesize_segments
from book_video_workbench.util import media_duration, read_json, utc_now, write_json


STYLE_PRESETS = {
    "clean-narration": {"label": "清雅语录", "motion": "cinematic"},
    "typewriter-dark": {"label": "黑底打字机", "motion": "typewriter"},
    "dark-knowledge": {"label": "暗色知识卡", "motion": "slow-zoom"},
    "book-broadcast": {"label": "图书口播卡", "motion": "quick-cut"},
    "book-sales": {"label": "强转化图书", "motion": "sales-cut"},
}

SALES_VISUAL_STYLES = {"book-sales", "book-broadcast"}


def primary_visual_style(styles: list[str], style_counts: dict[str, int]) -> str:
    selected = [
        style for style in styles if style in STYLE_PRESETS and int(style_counts.get(style, 1)) > 0
    ]
    for preferred in ("book-sales", "book-broadcast", "dark-knowledge", "typewriter-dark"):
        if preferred in selected:
            return preferred
    return selected[0] if selected else "book-sales"


def recommended_scene_count(duration: float, visual_style_id: str) -> int:
    seconds_per_scene = 3.6 if visual_style_id in SALES_VISUAL_STYLES else 4.5
    return max(6, min(63, math.ceil(duration / seconds_per_scene)))


@dataclass
class RunOptions:
    mode: str
    share_text: str
    book_title: str = ""
    selling_points: list[str] = field(default_factory=list)
    target_seconds: int = 90
    whisper_model: str = "small"
    subtitle_mode: str = "proportional"
    book_cover: str | None = None
    allow_source_video: bool = False
    keyword: str = "图书带货"
    book_author: str = ""
    rewrite_mode: str = "medium"
    rewrite_notes: str = "保留可核实事实，重组叙事角度、信息顺序和表达，避免同义词式洗稿"
    scene_count: int = 0
    styles: list[str] = field(default_factory=lambda: ["book-sales"])
    style_counts: dict[str, int] = field(default_factory=lambda: {"book-sales": 1})
    declaration: str = "本视频基于公开资料整理，仅作阅读分享，不构成医疗建议或行为指导。"


def create_task(settings: Settings, options: RunOptions) -> Path:
    day = datetime.now().strftime("%Y%m%d")
    task_id = f"{day}-{uuid.uuid4().hex[:10]}"
    task_dir = settings.data_dir / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=False)
    write_json(
        task_dir / "task.json",
        {
            "schema_version": 2,
            "id": task_id,
            "created_at": utc_now(),
            "options": asdict(options),
        },
    )
    PipelineState(task_dir)
    return task_dir


class Pipeline:
    def __init__(self, task_dir: Path, settings: Settings) -> None:
        self.task_dir = task_dir.resolve()
        self.settings = settings
        self.task_path = self.task_dir / "task.json"
        self.task = read_json(self.task_path)
        known = RunOptions.__dataclass_fields__
        options = {key: value for key, value in self.task.get("options", {}).items() if key in known}
        self.options = RunOptions(**options)
        self.state = PipelineState(self.task_dir)

    def _active(self, key: str, default: str = "") -> Path:
        relative = (self.task.get("active_artifacts") or {}).get(key, default)
        return self.task_dir / relative

    def _next_version(self, directory: str, prefix: str, suffix: str) -> Path:
        target_dir = self.task_dir / directory
        target_dir.mkdir(parents=True, exist_ok=True)
        versions = []
        for candidate in target_dir.glob(f"{prefix}*{suffix}"):
            middle = candidate.name[len(prefix) : -len(suffix)] if suffix else candidate.name[len(prefix) :]
            if middle.isdigit():
                versions.append(int(middle))
        return target_dir / f"{prefix}{max(versions, default=0) + 1}{suffix}"

    def _activate(self, **artifacts: Path) -> None:
        active = self.task.setdefault("active_artifacts", {})
        for key, path in artifacts.items():
            active[key] = str(path.resolve().relative_to(self.task_dir))
        self.task["updated_at"] = utc_now()
        write_json(self.task_path, self.task)

    def _ensure_ai_product_assets(
        self,
        book: dict,
        *,
        current_path: Path,
    ) -> tuple[dict, Path, list[Path]]:
        """Fill AI-derived selling points and cover without blocking the user.

        A manually supplied cover or selling points always win. For real tasks,
        missing values are prepared from the already identified source content.
        Each automatic update is versioned so the original identification
        artifact remains available for audit.
        """
        if self.options.mode == "demo":
            return book, current_path, []

        updated = dict(book)
        changed = False
        selling_points = [
            str(item).strip()
            for item in updated.get("selling_points") or []
            if str(item).strip()
        ]
        if len(selling_points) < 2:
            suggestions = [
                str(item).strip()
                for item in updated.get("suggested_selling_points") or []
                if str(item).strip()
            ]
            if len(suggestions) >= 2:
                selling_points = suggestions[:3]
                updated["selling_points"] = selling_points
                updated["selling_points_source"] = "ai_extracted_from_source"
                changed = True

        cover_value = str(updated.get("book_cover") or "").strip()
        cover_path = Path(cover_value).expanduser().resolve() if cover_value else None
        generated_outputs: list[Path] = []
        if not cover_path or not cover_path.is_file():
            title = str(updated.get("book_title") or "").strip()
            if title:
                cover_path = self._next_version("book/assets", "ai-cover-v", ".jpg")
                cover_metadata = cover_path.with_suffix(".json")
                generate_ai_book_cover(
                    book_title=title,
                    book_author=str(updated.get("book_author") or ""),
                    selling_points=selling_points,
                    output_path=cover_path,
                    metadata_path=cover_metadata,
                    settings=self.settings,
                )
                updated["book_cover"] = str(cover_path.resolve())
                updated["book_cover_source"] = "ai_generated"
                generated_outputs.extend([cover_path, cover_metadata])
                changed = True

        updated["asset_warnings"] = product_asset_warnings(
            book_author=str(updated.get("book_author") or ""),
            selling_points=selling_points,
            book_cover=str(updated.get("book_cover") or ""),
        )
        updated["product_ready"] = product_is_ready(
            book_title=str(updated.get("book_title") or ""),
            selling_points=selling_points,
            book_cover=str(updated.get("book_cover") or ""),
        )
        updated["automation"] = {
            "selling_points": updated.get("selling_points_source") == "ai_extracted_from_source",
            "book_cover": updated.get("book_cover_source") == "ai_generated",
        }
        if not changed and updated == book:
            return book, current_path, generated_outputs

        identity_path = self._next_version("book", "identity-v", ".json")
        write_json(identity_path, updated)
        self.task["options"]["selling_points"] = selling_points
        self.task["options"]["book_cover"] = updated.get("book_cover") or None
        self._activate(book_info=identity_path)
        return updated, identity_path, [identity_path, *generated_outputs]

    def run(self, force_stage: str | None = None) -> Path:
        if force_stage:
            if force_stage not in STAGES:
                raise RuntimeError(f"未知阶段: {force_stage}")
            self.state.invalidate_from(force_stage)
        for stage_name in STAGES:
            if self.state.stage_complete(stage_name):
                continue
            getattr(self, f"_stage_{stage_name}")()
        return self._active("final_video", "renders/final-v1.mp4")

    def _stage_repair(self) -> None:
        with self.state.running("repair") as outputs:
            source_dir = self.task_dir / "source"
            source_dir.mkdir(parents=True, exist_ok=True)
            if self.options.mode == "demo":
                meta = write_json(
                    source_dir / "meta.normalized.json",
                    {
                        "platform": "demo",
                        "external_id": "article-flow-demo",
                        "title": "《把时间当作朋友》时间不会因为焦虑而变多",
                        "description": "文章同款图书带货长视频流程离线样片",
                        "author": "参考流程样片",
                        "source_url": None,
                        "duration_seconds": 73.57,
                        "metrics": {
                            "like": {"value": 17361},
                            "comment": {"value": 138},
                            "share": {"value": 3858},
                        },
                    },
                )
                raw = write_json(
                    self._next_version("transcript", "raw-v", ".json"),
                    {
                        "schema_version": 1,
                        "language": "zh",
                        "model": "article-flow-demo",
                        "full_text": DEMO_RAW_TRANSCRIPT,
                        "segments": [],
                    },
                )
            else:
                raw_meta, meta, video = capture_douyin(
                    self.options.share_text, self.task_dir, self.settings
                )
                audio = source_dir / "audio.wav"
                extract_audio(video, audio)
                raw = transcribe_media(
                    audio,
                    self._next_version("transcript", "raw-v", ".json"),
                    model_size=self.options.whisper_model,
                    include_words=True,
                )
                outputs.extend([raw_meta, video, audio])
            meta_value = read_json(meta)
            raw_value = read_json(raw)
            repaired = repair_transcript(
                raw_value["full_text"],
                keyword=self.options.keyword,
                title=meta_value.get("title", ""),
                author=meta_value.get("author", ""),
                settings=self.settings,
                output_path=self._next_version("transcript", "repaired-v", ".json"),
                demo=self.options.mode == "demo",
            )
            self._activate(raw_transcript=raw, repaired_transcript=repaired)
            outputs.extend([meta, raw, repaired])

    def _stage_rewrite(self) -> None:
        with self.state.running("rewrite") as outputs:
            meta = read_json(self.task_dir / "source" / "meta.normalized.json")
            repaired = read_json(self._active("repaired_transcript"))
            book = read_json(self._active("book_info"))
            candidates = rewrite_candidates(
                repaired["cleaned_text"],
                keyword=self.options.keyword,
                title=meta.get("title", ""),
                author=meta.get("author", ""),
                notes=self.options.rewrite_notes,
                settings=self.settings,
                output_path=self._next_version("scripts", "candidates-v", ".json"),
                book_title=book.get("book_title") or self.options.book_title,
                book_author=book.get("book_author") or self.options.book_author,
                selling_points=book.get("selling_points") or self.options.selling_points,
                target_seconds=self.options.target_seconds,
                rewrite_mode=self.options.rewrite_mode,
                demo=self.options.mode == "demo",
            )
            candidate_data = read_json(candidates)
            candidate = max(
                candidate_data["candidates"],
                key=lambda item: item.get("quality", {}).get("overall_score", 0),
            )
            selected = write_json(
                self._next_version("scripts", "selected-v", ".json"),
                {
                    "schema_version": 2,
                    "candidate_id": candidate["id"],
                    "label": candidate["label"],
                    "hook": candidate["hook"],
                    "script": candidate["script"],
                    "quality": candidate.get("quality", {}),
                    "selection_reason": "自动采用综合质量评分最高的候选",
                    "selected_at": utc_now(),
                },
            )
            self._activate(rewrite_candidates=candidates, selected_script=selected)
            outputs.extend([candidates, selected])

    def _stage_audio(self) -> None:
        with self.state.running("audio") as outputs:
            meta = read_json(self.task_dir / "source" / "meta.normalized.json")
            selected = read_json(self._active("selected_script"))
            segments, segment_prompt = split_narration_with_article_prompt(
                selected["script"],
                keyword=self.options.keyword,
                title=meta.get("title", ""),
                author=meta.get("author", ""),
                settings=self.settings,
                demo=self.options.mode == "demo",
            )
            plan = write_json(
                self._next_version("tts", "plan-v", ".json"),
                {
                    "schema_version": 1,
                    "target_segment_seconds": 26,
                    "prompt": segment_prompt,
                    "segments": [
                        {"index": index, "text": text, "estimated_seconds": round(len(text) / 4.2)}
                        for index, text in enumerate(segments, start=1)
                    ],
                },
            )
            audio = self._next_version("tts", "v", ".wav")
            metadata = audio.with_suffix(".json")
            audio, metadata, parts = synthesize_segments(
                segments,
                output_path=audio,
                metadata_path=metadata,
                settings=self.settings,
                demo=self.options.mode == "demo",
            )
            duration = media_duration(audio)
            timeline = proportional_timeline(selected["script"], duration)
            subtitle_json = self._next_version("subtitles", "v", ".json")
            subtitle_srt = subtitle_json.with_suffix(".srt")
            write_subtitles(
                timeline,
                duration,
                subtitle_json,
                subtitle_srt,
                mode="tts-script-proportional",
            )
            self._activate(
                tts_plan=plan,
                tts_audio=audio,
                tts_metadata=metadata,
                subtitle_json=subtitle_json,
                subtitle_srt=subtitle_srt,
            )
            outputs.extend([plan, audio, metadata, subtitle_json, subtitle_srt, *parts])

    def _stage_scene_images(self) -> None:
        with self.state.running("scene_images") as outputs:
            selected = read_json(self._active("selected_script"))
            book_path = self._active("book_info")
            book = read_json(book_path)
            book, book_path, product_outputs = self._ensure_ai_product_assets(
                book,
                current_path=book_path,
            )
            outputs.extend(product_outputs)
            visual_style_id = primary_visual_style(
                self.options.styles, self.options.style_counts
            )
            if (
                self.options.mode != "demo"
                and visual_style_id in SALES_VISUAL_STYLES
                and not book.get("product_ready")
            ):
                raise RuntimeError(
                    "AUTO_PRODUCT_ASSETS_UNAVAILABLE: 系统未能自动准备书名、AI 封面和至少 2 条内容卖点。"
                )
            audio_duration = media_duration(self._active("tts_audio"))
            effective_count = (
                recommended_scene_count(audio_duration, visual_style_id)
                if self.options.scene_count == 0
                else self.options.scene_count
            )
            version_path = self._next_version("scene-images", "manifest-v", ".json")
            version = int(version_path.stem.replace("manifest-v", ""))
            cover_value = book.get("book_cover") or self.options.book_cover
            manifest, contact_sheets, scenes = generate_scene_images(
                selected["script"],
                count=effective_count,
                task_dir=self.task_dir,
                version=version,
                settings=self.settings,
                demo=self.options.mode == "demo",
                book_title=book.get("book_title") or self.options.book_title,
                book_author=book.get("book_author") or self.options.book_author,
                selling_points=book.get("selling_points") or self.options.selling_points,
                book_cover=cover_value,
                product_ready=bool(book.get("product_ready")),
                visual_style_id=visual_style_id,
                auto_regenerate=self.options.mode != "demo",
                requested_count=self.options.scene_count,
            )
            # Keep a stable version pointer alongside the generated directory manifest.
            write_json(version_path, read_json(manifest))
            self._activate(scene_manifest=manifest, scene_version_manifest=version_path)
            outputs.extend([manifest, version_path, *contact_sheets, *scenes])

    def _stage_book_info(self) -> None:
        with self.state.running("book_info") as outputs:
            meta = read_json(self.task_dir / "source" / "meta.normalized.json")
            repaired = read_json(self._active("repaired_transcript"))
            book_path = identify_book(
                repaired["cleaned_text"],
                existing_title=self.options.book_title,
                existing_author=self.options.book_author,
                keyword=self.options.keyword,
                source_title=meta.get("title", ""),
                source_description=meta.get("description", ""),
                settings=self.settings,
                output_path=self._next_version("book", "identity-v", ".json"),
                selling_points=self.options.selling_points,
                book_cover=self.options.book_cover,
                demo=self.options.mode == "demo",
            )
            self._activate(book_info=book_path)
            outputs.append(book_path)
            book, active_book_path, product_outputs = self._ensure_ai_product_assets(
                read_json(book_path),
                current_path=book_path,
            )
            outputs.extend(product_outputs)
            if active_book_path == book_path:
                self._activate(book_info=book_path)

    def _stage_styles(self) -> None:
        with self.state.running("styles") as outputs:
            invalid = [style for style in self.options.styles if style not in STYLE_PRESETS]
            if invalid:
                raise RuntimeError("未知成片风格: " + ", ".join(invalid))
            selected_styles = self.options.styles or ["book-sales"]
            counts = {
                style: max(0, min(5, int(self.options.style_counts.get(style, 1))))
                for style in selected_styles
            }
            counts = {style: count for style, count in counts.items() if count > 0}
            if not counts:
                counts = {"book-sales": 1}
                selected_styles = ["book-sales"]
            config = write_json(
                self._next_version("styles", "v", ".json"),
                {
                    "schema_version": 1,
                    "selected": selected_styles,
                    "presets": {key: STYLE_PRESETS[key] for key in selected_styles},
                    "primary_visual_style": primary_visual_style(selected_styles, counts),
                    "counts": counts,
                    "declaration": self.options.declaration,
                    "output_count": sum(counts.values()),
                },
            )
            self._activate(style_config=config)
            outputs.append(config)

    def _stage_outputs(self) -> None:
        with self.state.running("outputs") as outputs:
            meta = read_json(self.task_dir / "source" / "meta.normalized.json")
            selected = read_json(self._active("selected_script"))
            tts_audio = self._active("tts_audio")
            subtitle_json = self._active("subtitle_json")
            scene_manifest = read_json(self._active("scene_manifest"))
            scene_images = [Path(path) for path in scene_manifest["scenes"]]
            briefs = scene_manifest["briefs"]
            book = read_json(self._active("book_info"))
            style_config = read_json(self._active("style_config"))
            book, _, product_outputs = self._ensure_ai_product_assets(
                book,
                current_path=self._active("book_info"),
            )
            outputs.extend(product_outputs)
            if (
                self.options.mode != "demo"
                and any(style in SALES_VISUAL_STYLES for style in style_config.get("selected", []))
                and not book.get("product_ready")
            ):
                raise RuntimeError(
                    "AUTO_PRODUCT_ASSETS_UNAVAILABLE: 系统未能自动准备书名、AI 封面和至少 2 条内容卖点。"
                )
            duration = media_duration(tts_audio)
            cover_value = book.get("book_cover") or self.options.book_cover
            cover = Path(cover_value).expanduser().resolve() if cover_value else None
            if cover and not cover.is_file():
                raise RuntimeError(f"书籍封面不存在: {cover}")
            rendered: list[dict] = []
            render_jobs = [
                (style_id, variant)
                for style_id, count in style_config.get("counts", {}).items()
                for variant in range(1, count + 1)
            ]
            for style_id, variant in render_jobs:
                plan_dir = self.task_dir / "output-plans" / style_id
                storyboard, manifest = build_storyboard(
                    self.task_dir,
                    title=meta.get("title", ""),
                    book_title=book.get("book_title") or self.options.book_title or "待确认书名",
                    book_author=book.get("book_author") or self.options.book_author,
                    hook=selected.get("hook") or selected["script"][:24],
                    duration=duration,
                    subtitle_path=subtitle_json,
                    tts_path=tts_audio,
                    source_video=None,
                    book_cover=cover,
                    scene_images=scene_images,
                    scene_briefs=briefs,
                    style_id=style_id,
                    selling_points=book.get("selling_points") or self.options.selling_points,
                    declaration=style_config["declaration"],
                    version_path=self._next_version(str(plan_dir.relative_to(self.task_dir)), "storyboard-v", ".json"),
                    manifest_path=self._next_version(str(plan_dir.relative_to(self.task_dir)), "manifest-v", ".json"),
                )
                project = generate_project(
                    manifest,
                    self.task_dir,
                    self.settings,
                    project_name=f"render-project-{style_id}-{variant}",
                )
                video = self._next_version("renders", f"{style_id}-v", ".mp4")
                render_project(project, video, self.settings)
                report = validate_video(video, video.with_suffix(".ffprobe.json"))
                rendered.append(
                    {
                        "style_id": style_id,
                        "style_label": STYLE_PRESETS[style_id]["label"],
                        "variant": variant,
                        "video_path": str(video.resolve()),
                        "validation_path": str(report.resolve()),
                    }
                )
                outputs.extend([storyboard, manifest, project / "index.html", video, report])
            index = write_json(
                self._next_version("renders", "outputs-v", ".json"),
                {"schema_version": 1, "outputs": rendered},
            )
            first_video = Path(rendered[0]["video_path"])
            self._activate(output_index=index, final_video=first_video)
            outputs.append(index)

    def _stage_review(self) -> None:
        with self.state.running("review") as outputs:
            book = read_json(self._active("book_info"))
            candidates = read_json(self._active("rewrite_candidates"))
            scene_manifest = read_json(self._active("scene_manifest"))
            output_index = read_json(self._active("output_index"))
            selected_id = read_json(self._active("selected_script")).get("candidate_id")
            selected_candidate = next(
                (
                    item
                    for item in candidates.get("candidates", [])
                    if item.get("id") == selected_id
                ),
                {},
            )
            scene_quality = scene_manifest.get("quality", {})
            needs_review = bool(
                book.get("needs_review")
                or not book.get("product_ready")
                or book.get("asset_warnings")
                or selected_candidate.get("findings")
                or scene_quality.get("status") == "needs_review"
            )
            review = write_json(
                self._next_version("review", "v", ".json"),
                {
                    "schema_version": 1,
                    "status": "needs_review" if needs_review else "ready",
                    "checks": {
                        "book_identity": "needs_review" if book.get("needs_review") else "passed",
                        "product_assets": "passed" if book.get("product_ready") else "blocked",
                        "copy_quality": selected_candidate.get("quality", {}).get("overall_score", "needs_review"),
                        "scene_images": scene_quality.get("status", "needs_review"),
                        "compliance": "manual_confirmation_recommended",
                        "outputs": len(output_index["outputs"]),
                    },
                    "book": book,
                    "outputs": output_index["outputs"],
                },
            )
            self._activate(review_report=review)
            outputs.append(review)

from __future__ import annotations

import math
from pathlib import Path

from book_video_workbench.util import write_json


def build_storyboard(
    task_dir: Path,
    *,
    title: str,
    book_title: str,
    hook: str,
    duration: float,
    subtitle_path: Path,
    tts_path: Path,
    source_video: Path | None,
    book_cover: Path | None,
    scene_images: list[Path] | None = None,
    scene_briefs: list[dict] | None = None,
    style_id: str = "clean-narration",
    book_author: str = "",
    selling_points: list[str] | None = None,
    declaration: str = "本视频基于公开资料整理，仅作阅读分享，不构成医疗建议。",
    version_path: Path | None = None,
    manifest_path: Path | None = None,
) -> tuple[Path, Path]:
    scene_count = len(scene_images or []) or max(3, math.ceil(duration / 4.0))
    boundaries = [round(duration * index / scene_count, 3) for index in range(scene_count)]
    boundaries.append(round(duration, 3))
    palette = [
        {"background": "#16191d", "accent": "#ff5a4f", "foreground": "#f7f4ed"},
        {"background": "#ece5cf", "accent": "#c42d32", "foreground": "#211f23"},
        {"background": "#103f46", "accent": "#ffd05a", "foreground": "#f5f1e8"},
        {"background": "#27262f", "accent": "#70d6c8", "foreground": "#f7f4ed"},
    ]
    selling_points = [
        str(item).strip() for item in selling_points or [] if str(item).strip()
    ]
    labels = [hook, book_title, *(selling_points[:2] or ["这本书解决什么问题"]), "适合你吗？"]
    product_roles = {"product_space", "product_reveal", "product_detail", "closing"}
    scenes = []
    for index in range(scene_count):
        start = boundaries[index]
        end = boundaries[index + 1]
        brief = (scene_briefs or [{}])[index % max(1, len(scene_briefs or [{}]))]
        shot_role = brief.get("shot_role", "editorial_symbol")
        if index == 0:
            headline = hook
        elif shot_role in product_roles:
            headline = selling_points[(index - 1) % len(selling_points)] if selling_points else book_title
        elif index == scene_count - 1:
            headline = "适合你吗？"
        else:
            headline = labels[index % len(labels)]
        scenes.append(
            {
                "id": f"scene-{index + 1}",
                "start": start,
                "duration": round(end - start, 3),
                "headline": headline,
                "script_text": brief.get("script_text", ""),
                "shot_role": shot_role,
                "product_scene": shot_role in product_roles,
                "visual_purpose": brief.get("visual_purpose", "推进图书知识叙事"),
                "image_path": str(scene_images[index].resolve()) if scene_images else None,
                **palette[index % len(palette)],
            }
        )
    storyboard_path = write_json(
        version_path or task_dir / "storyboard" / "v1.json",
        {
            "schema_version": 1,
            "template_id": style_id,
            "duration_seconds": round(duration, 3),
            "scenes": scenes,
            "source_video_enabled": bool(source_video),
            "book_cover_enabled": bool(book_cover),
        },
    )
    manifest = {
        "schema_version": 1,
        "canvas": {"width": 1080, "height": 1920, "fps": 30},
        "duration_seconds": round(duration, 3),
        "template": {"id": style_id, "version": 2},
        "content": {
            "source_title": title,
            "book_title": book_title,
            "book_author": book_author,
            "hook": hook,
            "declaration": declaration,
            "selling_points": selling_points,
        },
        "audio": {"path": str(tts_path.resolve()), "volume": 1.0},
        "subtitles": {"path": str(subtitle_path.resolve()), "style": "bold-safe-zone-v1"},
        "scenes": scenes,
        "assets": {
            "source_video": str(source_video.resolve()) if source_video else None,
            "book_cover": str(book_cover.resolve()) if book_cover else None,
            "scene_images": [str(path.resolve()) for path in scene_images or []],
        },
    }
    written_manifest = write_json(
        manifest_path or task_dir / "render-manifest.json", manifest
    )
    return storyboard_path, written_manifest

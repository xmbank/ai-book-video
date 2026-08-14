from __future__ import annotations

import base64
import io
import json
import math
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageOps

from book_video_workbench.article_prompts import (
    IMAGE_PROMPT_VERSION,
    IMAGE_STYLE_BIBLE,
    PROMPT_SOURCE,
    build_image_prompt,
)
from book_video_workbench.config import Settings
from book_video_workbench.content_flow import scene_briefs
from book_video_workbench.util import require_command, run_command, write_json


STYLE_BIBLE = IMAGE_STYLE_BIBLE


def _cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    return ImageOps.fit(image.convert("RGB"), size, method=Image.Resampling.LANCZOS)


def compose_grid(images: list[Path], output_path: Path) -> Path:
    cell_size = (512, 288)
    canvas = Image.new("RGB", (cell_size[0] * 3, cell_size[1] * 3), "white")
    for index, path in enumerate(images[:9]):
        with Image.open(path) as source:
            cell = _cover(source, cell_size)
        canvas.paste(cell, ((index % 3) * cell_size[0], (index // 3) * cell_size[1]))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, quality=92)
    return output_path


def split_grid(grid_path: Path, output_dir: Path, *, start_index: int) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    with Image.open(grid_path) as source:
        grid = _cover(source, (1536, 864))
        for index in range(9):
            left = (index % 3) * 512
            top = (index // 3) * 288
            cell = grid.crop((left, top, left + 512, top + 288))
            portrait = _cover(cell, (720, 1280))
            path = output_dir / f"scene-{start_index + index:03}.jpg"
            portrait.save(path, quality=92)
            outputs.append(path)
    return outputs


def _extract_demo_frames(video_path: Path, output_dir: Path, count: int) -> list[Path]:
    if not video_path.is_file():
        raise RuntimeError(f"离线参考成片不存在: {video_path}")
    ffmpeg = require_command("ffmpeg")
    output_dir.mkdir(parents=True, exist_ok=True)
    # The bundled reference is 73 seconds; avoid title/end cards when sampling.
    positions = [3 + index * 66 / max(1, count - 1) for index in range(count)]
    frames = []
    for index, position in enumerate(positions, start=1):
        path = output_dir / f"frame-{index:03}.jpg"
        run_command(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{position:.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(path),
            ]
        )
        # The bundled reference video already has yellow captions. Crop the lower band
        # so the offline demo verifies image composition without double subtitles.
        with Image.open(path) as source:
            clean = source.convert("RGB").crop((0, 0, source.width, int(source.height * 0.78)))
            clean = _cover(clean, (720, 1280))
            clean.save(path, quality=92)
        frames.append(path)
    return frames


def _generate_grid_api(prompt: str, output_path: Path, settings: Settings) -> Path:
    if not settings.image_api_key:
        raise RuntimeError("真实 AI 场景图需要配置 IMAGE_API_KEY")
    payload = {
        "model": settings.image_model,
        "prompt": prompt,
        "size": settings.image_size,
        "n": 1,
    }
    request = urllib.request.Request(
        f"{settings.image_base_url}/images/generations",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.image_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"图片生成失败 ({exc.code}): {body[:1200]}") from exc
    item = (result.get("data") or [{}])[0]
    if item.get("b64_json"):
        raw = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        with urllib.request.urlopen(item["url"], timeout=180) as response:
            raw = response.read()
    else:
        raise RuntimeError("图片生成接口没有返回 b64_json 或 url")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(raw)) as image:
        image.convert("RGB").save(output_path, quality=94)
    return output_path


def generate_scene_images(
    script_text: str,
    *,
    count: int,
    task_dir: Path,
    version: int,
    settings: Settings,
    demo: bool,
) -> tuple[Path, list[Path], list[Path]]:
    if count not in {9, 18, 27, 36, 45, 54, 63}:
        raise RuntimeError("场景图数量必须是 9 到 63 的九的倍数")
    root = task_dir / "scene-images" / f"v{version}"
    grids_dir = root / "grids"
    scenes_dir = root / "scenes"
    briefs = scene_briefs(script_text, count)
    grids: list[Path] = []
    scenes: list[Path] = []
    prompt_snapshots: list[dict[str, str | int]] = []
    if demo:
        frames = _extract_demo_frames(settings.reference_demo_video, root / "demo-frames", count)
        for group_index in range(math.ceil(count / 9)):
            group = frames[group_index * 9 : group_index * 9 + 9]
            while len(group) < 9:
                group.append(group[-1])
            grid = compose_grid(group, grids_dir / f"grid-{group_index + 1:02}.jpg")
            grids.append(grid)
            scenes.extend(split_grid(grid, scenes_dir, start_index=group_index * 9 + 1))
    else:
        for group_index in range(math.ceil(count / 9)):
            group = briefs[group_index * 9 : group_index * 9 + 9]
            prompt, snapshot = build_image_prompt(
                [str(item["visual_brief"]) for item in group]
            )
            prompt_snapshots.append({"grid_index": group_index + 1, **snapshot})
            grid = _generate_grid_api(
                prompt, grids_dir / f"grid-{group_index + 1:02}.jpg", settings
            )
            grids.append(grid)
            scenes.extend(split_grid(grid, scenes_dir, start_index=group_index * 9 + 1))
    scenes = scenes[:count]
    for brief, scene in zip(briefs, scenes):
        brief["image_path"] = str(scene.resolve())
    manifest = write_json(
        root / "manifest.json",
        {
            "schema_version": 1,
            "count": count,
            "mode": "demo-reference-frames" if demo else "api-grid-generation",
            "grid_count": len(grids),
            "prompt_version": IMAGE_PROMPT_VERSION,
            "prompt_source": PROMPT_SOURCE,
            "style_bible": STYLE_BIBLE,
            "prompt_snapshots": prompt_snapshots,
            "briefs": briefs,
            "grids": [str(path.resolve()) for path in grids],
            "scenes": [str(path.resolve()) for path in scenes],
        },
    )
    return manifest, grids, scenes

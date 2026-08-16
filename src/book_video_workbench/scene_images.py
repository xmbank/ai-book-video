from __future__ import annotations

import base64
import io
import json
import math
import re
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageOps, ImageStat

from book_video_workbench.article_prompts import (
    IMAGE_PROMPT_VERSION,
    IMAGE_STYLE_BIBLE,
    PROMPT_SOURCE,
    build_image_prompt,
)
from book_video_workbench.config import Settings
from book_video_workbench.content_flow import direct_scene_briefs
from book_video_workbench.util import require_command, run_command, write_json


STYLE_BIBLE = IMAGE_STYLE_BIBLE
SCENE_SIZE = (720, 1280)
CONTACT_CELL_SIZE = (240, 427)
IMAGE_REQUEST_ATTEMPTS = 4
IMAGE_RETRY_DELAYS = (1.0, 2.0, 4.0)
TRANSIENT_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}


def _cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    return ImageOps.fit(image.convert("RGB"), size, method=Image.Resampling.LANCZOS)


def compose_grid(images: list[Path], output_path: Path) -> Path:
    """Build a preview contact sheet from real portrait scenes.

    The model never generates this sheet. It exists only for fast review, so its
    boundaries are deterministic and can never leak into production frames.
    """
    canvas = Image.new(
        "RGB",
        (CONTACT_CELL_SIZE[0] * 3, CONTACT_CELL_SIZE[1] * 3),
        "#f3efe5",
    )
    if not images:
        raise RuntimeError("联系表至少需要一张竖屏图")
    padded = list(images[:9])
    while len(padded) < 9:
        padded.append(padded[-1])
    for index, path in enumerate(padded):
        with Image.open(path) as source:
            cell = _cover(source, CONTACT_CELL_SIZE)
        canvas.paste(
            cell,
            (
                (index % 3) * CONTACT_CELL_SIZE[0],
                (index // 3) * CONTACT_CELL_SIZE[1],
            ),
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, quality=92)
    return output_path


def split_grid(grid_path: Path, output_dir: Path, *, start_index: int) -> list[Path]:
    """Legacy helper for deterministic portrait contact sheets only.

    New production never splits an AI-generated grid. Keeping this helper makes
    old tooling readable without reintroducing landscape-to-portrait cropping.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    expected = (CONTACT_CELL_SIZE[0] * 3, CONTACT_CELL_SIZE[1] * 3)
    with Image.open(grid_path) as source:
        grid = _cover(source, expected)
        for index in range(9):
            left = (index % 3) * CONTACT_CELL_SIZE[0]
            top = (index // 3) * CONTACT_CELL_SIZE[1]
            cell = grid.crop(
                (
                    left,
                    top,
                    left + CONTACT_CELL_SIZE[0],
                    top + CONTACT_CELL_SIZE[1],
                )
            )
            path = output_dir / f"scene-{start_index + index:03}.jpg"
            _cover(cell, SCENE_SIZE).save(path, quality=92)
            outputs.append(path)
    return outputs


def _extract_demo_frames(video_path: Path, output_dir: Path, count: int) -> list[Path]:
    if not video_path.is_file():
        raise RuntimeError(f"离线参考成片不存在: {video_path}")
    ffmpeg = require_command("ffmpeg")
    output_dir.mkdir(parents=True, exist_ok=True)
    positions = [3 + index * 66 / max(1, count - 1) for index in range(count)]
    frames = []
    for index, position in enumerate(positions, start=1):
        path = output_dir / f"scene-{index:03}.jpg"
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
        with Image.open(path) as source:
            clean = source.convert("RGB").crop(
                (0, 0, source.width, int(source.height * 0.78))
            )
            _cover(clean, SCENE_SIZE).save(path, quality=92)
        frames.append(path)
    return frames


def _portrait_request_size(configured: str) -> str:
    match = re.fullmatch(r"\s*(\d+)x(\d+)\s*", configured or "")
    if not match:
        return "1024x1536"
    width, height = (int(value) for value in match.groups())
    if height > width:
        return f"{width}x{height}"
    # OpenAI-compatible providers commonly accept this portrait size. More
    # specialized providers can explicitly configure another portrait size.
    return "1024x1536"


def _read_remote_bytes(
    request: urllib.request.Request | str,
    *,
    timeout: int,
    operation: str,
) -> bytes:
    """Read a remote image response with bounded retries for transient failures."""
    last_error = "未知网络错误"
    for attempt in range(1, IMAGE_REQUEST_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code not in TRANSIENT_HTTP_CODES:
                raise RuntimeError(
                    f"{operation}失败 ({exc.code}): {body[:1200]}"
                ) from exc
            last_error = f"HTTP {exc.code}: {body[:600]}"
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            ssl.SSLError,
        ) as exc:
            last_error = str(exc)

        if attempt < IMAGE_REQUEST_ATTEMPTS:
            time.sleep(IMAGE_RETRY_DELAYS[attempt - 1])

    raise RuntimeError(
        "IMAGE_GENERATION_TRANSIENT_ERROR: "
        f"{operation}连接连续中断（已自动重试 {IMAGE_REQUEST_ATTEMPTS} 次）: "
        f"{last_error}"
    )


def _generate_image_api(
    prompt: str,
    output_path: Path,
    settings: Settings,
    *,
    size: str,
) -> Path:
    if not settings.image_api_key:
        raise RuntimeError("真实 AI 场景图需要配置 IMAGE_API_KEY")
    payload = {
        "model": settings.image_model,
        "prompt": prompt,
        "size": size,
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
    response_body = _read_remote_bytes(
        request,
        timeout=300,
        operation="图片生成",
    )
    try:
        result = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("图片生成接口返回了无法解析的数据") from exc
    item = (result.get("data") or [{}])[0]
    if item.get("b64_json"):
        raw = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        raw = _read_remote_bytes(
            item["url"],
            timeout=180,
            operation="图片结果下载",
        )
    else:
        raise RuntimeError("图片生成接口没有返回 b64_json 或 url")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(raw)) as image:
        _cover(image, SCENE_SIZE).save(output_path, quality=94)
    return output_path


def _reuse_existing_scene(path: Path) -> bool:
    """Return whether an existing checkpoint image is readable and reusable."""
    if not path.is_file():
        return False
    try:
        with Image.open(path) as existing:
            existing.load()
            normalized = _cover(existing, SCENE_SIZE)
        normalized.save(path, quality=94)
    except (OSError, ValueError):
        return False
    return True


def _average_hash(path: Path) -> int:
    with Image.open(path) as image:
        gray = image.convert("L").resize((8, 8), Image.Resampling.LANCZOS)
        values = list(gray.get_flattened_data())
    average = sum(values) / len(values)
    result = 0
    for value in values:
        result = (result << 1) | int(value >= average)
    return result


def _hash_similarity(left: int, right: int) -> float:
    return 1 - ((left ^ right).bit_count() / 64)


def inspect_scene_image(
    path: Path,
    *,
    previous_hashes: list[int] | None = None,
) -> dict[str, object]:
    previous_hashes = previous_hashes or []
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        stat = ImageStat.Stat(rgb.resize((64, 64), Image.Resampling.LANCZOS))
        contrast = round(sum(stat.stddev) / 3, 2)
        width, height = rgb.size
    current_hash = _average_hash(path)
    similarities = [_hash_similarity(current_hash, value) for value in previous_hashes]
    max_similarity = round(max(similarities, default=0.0), 3)
    flags = []
    if (width, height) != SCENE_SIZE:
        flags.append("invalid_dimensions")
    if contrast < 8:
        flags.append("near_blank")
    if max_similarity >= 0.94:
        flags.append("near_duplicate")
    return {
        "width": width,
        "height": height,
        "contrast": contrast,
        "max_previous_similarity": max_similarity,
        "flags": flags,
        "average_hash": f"{current_hash:016x}",
    }


def generate_scene_images(
    script_text: str,
    *,
    count: int,
    task_dir: Path,
    version: int,
    settings: Settings,
    demo: bool,
    book_title: str = "",
    book_author: str = "",
    selling_points: list[str] | None = None,
    book_cover: str | None = None,
) -> tuple[Path, list[Path], list[Path]]:
    if not 6 <= count <= 63:
        raise RuntimeError("场景图数量必须在 6 到 63 之间")
    selling_points = [
        str(item).strip() for item in selling_points or [] if str(item).strip()
    ]
    cover_path = Path(book_cover).expanduser().resolve() if book_cover else None
    cover_available = bool(cover_path and cover_path.is_file())
    root = task_dir / "scene-images" / f"v{version}"
    contacts_dir = root / "contact-sheets"
    scenes_dir = root / "scenes"
    briefs, director_prompt = direct_scene_briefs(
        script_text,
        count=count,
        book_title=book_title,
        book_author=book_author,
        selling_points=selling_points,
        cover_available=cover_available,
        settings=settings,
        demo=demo,
    )
    scenes: list[Path] = []
    prompt_snapshots: list[dict[str, str | int]] = []
    quality: list[dict[str, object]] = []
    hashes: list[int] = []
    request_size = _portrait_request_size(settings.image_size)

    if demo:
        scenes = _extract_demo_frames(settings.reference_demo_video, scenes_dir, count)
    else:
        scenes_dir.mkdir(parents=True, exist_ok=True)
        for index, brief in enumerate(briefs, start=1):
            prompt, snapshot = build_image_prompt(
                brief,
                shot_index=index,
                shot_count=count,
                book_title=book_title,
                cover_available=cover_available,
            )
            prompt_snapshots.append({"scene_index": index, **snapshot})
            scene = scenes_dir / f"scene-{index:03}.jpg"
            if not _reuse_existing_scene(scene):
                _generate_image_api(
                    prompt,
                    scene,
                    settings,
                    size=request_size,
                )
            # Normalize at the orchestration boundary as well as in the
            # built-in provider adapter. This keeps downstream video layout
            # deterministic when a compatible provider returns a different
            # aspect ratio or a custom adapter writes its native dimensions.
            with Image.open(scene) as generated:
                normalized = _cover(generated, SCENE_SIZE)
            normalized.save(scene, quality=94)
            scenes.append(scene)
            report = inspect_scene_image(scene, previous_hashes=hashes)
            current_hash = int(str(report["average_hash"]), 16)
            hashes.append(current_hash)
            quality.append({"scene_index": index, **report})

    if demo:
        for index, scene in enumerate(scenes, start=1):
            report = inspect_scene_image(scene, previous_hashes=hashes)
            hashes.append(int(str(report["average_hash"]), 16))
            quality.append({"scene_index": index, **report})

    contact_sheets: list[Path] = []
    for group_index in range(math.ceil(count / 9)):
        group = scenes[group_index * 9 : group_index * 9 + 9]
        contact_sheets.append(
            compose_grid(
                group,
                contacts_dir / f"contact-{group_index + 1:02}.jpg",
            )
        )

    for brief, scene in zip(briefs, scenes):
        brief["image_path"] = str(scene.resolve())
    flagged = [item for item in quality if item["flags"]]
    manifest = write_json(
        root / "manifest.json",
        {
            "schema_version": 2,
            "count": count,
            "mode": "demo-reference-portrait" if demo else "api-portrait-generation",
            "request_size": request_size,
            "output_size": f"{SCENE_SIZE[0]}x{SCENE_SIZE[1]}",
            "grid_count": len(contact_sheets),
            "contact_sheet_count": len(contact_sheets),
            "prompt_version": IMAGE_PROMPT_VERSION,
            "prompt_source": PROMPT_SOURCE,
            "style_bible": STYLE_BIBLE,
            "director_prompt": director_prompt,
            "prompt_snapshots": prompt_snapshots,
            "briefs": briefs,
            # Keep the old key as a compatibility alias for the review UI.
            "grids": [str(path.resolve()) for path in contact_sheets],
            "contact_sheets": [str(path.resolve()) for path in contact_sheets],
            "scenes": [str(path.resolve()) for path in scenes],
            "quality": {
                "status": "passed" if not flagged else "needs_review",
                "flagged_count": len(flagged),
                "checks": quality,
            },
        },
    )
    return manifest, contact_sheets, scenes

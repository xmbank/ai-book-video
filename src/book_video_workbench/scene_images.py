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
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat

from book_video_workbench.article_prompts import (
    IMAGE_PROMPT_VERSION,
    IMAGE_STYLE_BIBLE,
    PROMPT_SOURCE,
    build_image_prompt,
)
from book_video_workbench.config import Settings
from book_video_workbench.content_flow import direct_scene_briefs, scene_briefs
from book_video_workbench.util import require_command, run_command, write_json


STYLE_BIBLE = IMAGE_STYLE_BIBLE
SCENE_SIZE = (720, 1280)
BOOK_COVER_SIZE = (900, 1350)
CONTACT_CELL_SIZE = (240, 427)
IMAGE_REQUEST_ATTEMPTS = 4
IMAGE_RETRY_DELAYS = (1.0, 2.0, 4.0)
IMAGE_QUALITY_REGENERATIONS = 1
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


def _request_generated_image(
    prompt: str,
    settings: Settings,
    *,
    size: str,
) -> bytes:
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
    return raw


def _generate_image_api(
    prompt: str,
    output_path: Path,
    settings: Settings,
    *,
    size: str,
) -> Path:
    raw = _request_generated_image(prompt, settings, size=size)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(raw)) as image:
        _cover(image, SCENE_SIZE).save(output_path, quality=94)
    return output_path


def _cjk_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("/System/Library/Fonts/STHeiti Medium.ttc") if bold else Path("/System/Library/Fonts/STHeiti Light.ttc"),
        Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def _wrap_cover_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    lines: list[str] = []
    current = ""
    for character in text.strip():
        candidate = current + character
        box = draw.textbbox((0, 0), candidate, font=font, stroke_width=1)
        if current and box[2] - box[0] > max_width:
            lines.append(current)
            current = character
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or ["图书精选"]


def generate_ai_book_cover(
    *,
    book_title: str,
    book_author: str,
    selling_points: list[str],
    output_path: Path,
    settings: Settings,
    metadata_path: Path | None = None,
) -> tuple[Path, Path | None]:
    """Generate cover artwork and add deterministic, readable title typography.

    The image model is deliberately asked for artwork without lettering. The
    exact title and author are then rendered locally so model spelling errors
    can never leak into the product card or final video.
    """
    title = book_title.strip()
    if not title:
        raise RuntimeError("AI_BOOK_COVER_TITLE_REQUIRED: 自动生成封面前需要先识别书名")
    points = [str(item).strip() for item in selling_points if str(item).strip()]
    prompt = (
        "Generate premium front-cover artwork for a Chinese nonfiction book. "
        "Flat 2:3 portrait cover artwork only, not a photographed book mockup. "
        "Do not draw any words, letters, numbers, logos, author names, ISBN, "
        "barcode, page edges, hands, or blank rectangular placeholders. "
        "Reserve calm negative space in the upper third for title typography. "
        "Use a polished editorial illustration, strong commercial contrast, "
        "layered depth, restrained cinematic lighting, and a coherent palette. "
        "Avoid organs, surgery, pills, syringes, hospital scenes, body horror, "
        "and medical promises. "
        f"Book topic: {title}. "
        f"Author context: {book_author.strip() or 'not provided'}. "
        f"Content themes: {'; '.join(points[:3]) or 'reading, learning, practical insight'}."
    )
    request_size = _portrait_request_size(settings.image_size)
    try:
        raw = _request_generated_image(prompt, settings, size=request_size)
    except RuntimeError as exc:
        raise RuntimeError(f"AI_BOOK_COVER_GENERATION_FAILED: {exc}") from exc
    with Image.open(io.BytesIO(raw)) as generated:
        artwork = ImageOps.fit(
            generated.convert("RGB"),
            BOOK_COVER_SIZE,
            method=Image.Resampling.LANCZOS,
        ).convert("RGBA")

    overlay = Image.new("RGBA", BOOK_COVER_SIZE, (0, 0, 0, 0))
    overlay_pixels = overlay.load()
    for y in range(BOOK_COVER_SIZE[1]):
        top_alpha = max(0, 188 - int(y * 0.36))
        bottom_alpha = max(0, int((y - 850) * 0.30))
        alpha = min(205, max(top_alpha, bottom_alpha))
        for x in range(BOOK_COVER_SIZE[0]):
            overlay_pixels[x, y] = (8, 14, 18, alpha)
    cover = Image.alpha_composite(artwork, overlay)
    draw = ImageDraw.Draw(cover)

    title_font_size = 112
    while True:
        title_font = _cjk_font(title_font_size, bold=True)
        title_lines = _wrap_cover_text(draw, title, title_font, 760)
        if len(title_lines) <= 3 or title_font_size <= 58:
            break
        title_font_size -= 8
    line_height = int(title_font_size * 1.25)
    title_y = 118
    for line in title_lines[:3]:
        box = draw.textbbox((0, 0), line, font=title_font, stroke_width=2)
        line_width = box[2] - box[0]
        draw.text(
            ((BOOK_COVER_SIZE[0] - line_width) / 2, title_y),
            line,
            font=title_font,
            fill=(255, 252, 241, 255),
            stroke_width=2,
            stroke_fill=(10, 18, 22, 170),
        )
        title_y += line_height

    accent_y = min(title_y + 24, 620)
    draw.rounded_rectangle((335, accent_y, 565, accent_y + 8), radius=4, fill=(239, 187, 58, 245))
    author = book_author.strip()
    if author:
        author_font = _cjk_font(34)
        author_lines = _wrap_cover_text(draw, author, author_font, 700)[:2]
        author_y = 1135 - (len(author_lines) - 1) * 43
        for line in author_lines:
            box = draw.textbbox((0, 0), line, font=author_font)
            draw.text(
                ((BOOK_COVER_SIZE[0] - (box[2] - box[0])) / 2, author_y),
                line,
                font=author_font,
                fill=(245, 244, 235, 235),
            )
            author_y += 43

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cover.convert("RGB").save(output_path, format="JPEG", quality=95, optimize=True)

    if metadata_path:
        write_json(
            metadata_path,
            {
                "schema_version": 1,
                "prompt_version": "ai-book-cover-v1",
                "provider_model": settings.image_model,
                "request_size": request_size,
                "output_size": f"{BOOK_COVER_SIZE[0]}x{BOOK_COVER_SIZE[1]}",
                "book_title": title,
                "book_author": book_author.strip(),
                "selling_points": points,
                "prompt": prompt,
                "text_rendering": "deterministic-local-overlay",
                "output_path": str(output_path.resolve()),
            },
        )
    return output_path, metadata_path


def compose_cover_closing_fallback(book_cover: Path, output_path: Path) -> Path:
    """Create a resilient closing frame from the already generated AI cover.

    This is only used when the image provider times out on the final scene
    after all earlier portrait scenes have succeeded. It avoids discarding a
    nearly complete batch while keeping the closing frame visually distinct.
    """
    with Image.open(book_cover) as source:
        cover = source.convert("RGB")
        background = ImageOps.fit(
            cover,
            SCENE_SIZE,
            method=Image.Resampling.LANCZOS,
        ).filter(ImageFilter.GaussianBlur(radius=34))
        darken = Image.new("RGBA", SCENE_SIZE, (7, 13, 17, 150))
        canvas = Image.alpha_composite(background.convert("RGBA"), darken)
        card = ImageOps.contain(
            cover,
            (500, 820),
            method=Image.Resampling.LANCZOS,
        )
    shadow = Image.new("RGBA", (card.width + 70, card.height + 70), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle(
        (28, 28, card.width + 42, card.height + 42),
        radius=24,
        fill=(0, 0, 0, 190),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=18))
    x = (SCENE_SIZE[0] - card.width) // 2
    y = (SCENE_SIZE[1] - card.height) // 2 - 18
    canvas.alpha_composite(shadow, (x - 35, y - 20))
    card_layer = Image.new("RGBA", card.size, (0, 0, 0, 0))
    card_layer.paste(card, (0, 0))
    canvas.alpha_composite(card_layer, (x, y))
    accent = ImageDraw.Draw(canvas)
    accent.rounded_rectangle((215, 1160, 505, 1170), radius=5, fill=(239, 187, 58, 245))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output_path, quality=95)
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
    visual_style_id: str = "clean-narration",
) -> dict[str, object]:
    previous_hashes = previous_hashes or []
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        stat = ImageStat.Stat(rgb.resize((64, 64), Image.Resampling.LANCZOS))
        contrast = round(sum(stat.stddev) / 3, 2)
        hsv_stat = ImageStat.Stat(
            rgb.convert("HSV").resize((64, 64), Image.Resampling.LANCZOS)
        )
        saturation = round(hsv_stat.mean[1], 2)
        brightness = round(hsv_stat.mean[2], 2)
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
    if visual_style_id in {"book-sales", "book-broadcast"}:
        if contrast < 24:
            flags.append("low_commercial_contrast")
        if saturation < 24:
            flags.append("low_color_energy")
    return {
        "width": width,
        "height": height,
        "contrast": contrast,
        "saturation": saturation,
        "brightness": brightness,
        "max_previous_similarity": max_similarity,
        "flags": flags,
        "average_hash": f"{current_hash:016x}",
    }


def audit_scene_plan(
    briefs: list[dict[str, Any]],
    *,
    cover_available: bool,
    product_ready: bool,
    visual_style_id: str,
) -> dict[str, object]:
    roles = [str(item.get("shot_role") or "") for item in briefs]
    product_roles = {"product_space", "product_reveal", "closing"}
    people_roles = {"pattern_interrupt", "pain_point", "human_action", "desired_outcome"}
    product_count = sum(role in product_roles for role in roles)
    people_count = sum(role in people_roles for role in roles)
    blank_markers = ("空白书", "空白封面", "无字书", "空白书页", "白色书")
    blank_book_scenes = [
        index
        for index, brief in enumerate(briefs, start=1)
        if any(
            marker in " ".join(str(brief.get(key) or "") for key in ("subject", "action"))
            for marker in blank_markers
        )
    ]
    max_static_streak = 0
    current_static_streak = 0
    for role in roles:
        if role in {"detail", "proof", "product_space"}:
            current_static_streak += 1
            max_static_streak = max(max_static_streak, current_static_streak)
        else:
            current_static_streak = 0
    failures: list[str] = []
    if blank_book_scenes:
        failures.append("blank_book_placeholder")
    if visual_style_id in {"book-sales", "book-broadcast"}:
        minimum_product = max(2, math.ceil(len(briefs) * 0.18))
        minimum_people = max(2, math.ceil(len(briefs) * 0.20))
        if product_count < minimum_product:
            failures.append("insufficient_product_exposure")
        if people_count < minimum_people:
            failures.append("insufficient_human_emotion")
        if max_static_streak > 3:
            failures.append("too_many_consecutive_static_shots")
        if not cover_available or not product_ready:
            failures.append("product_assets_not_ready")
    return {
        "status": "passed" if not failures else "needs_review",
        "failures": failures,
        "product_scene_count": product_count,
        "human_emotion_scene_count": people_count,
        "blank_book_scenes": blank_book_scenes,
        "max_static_streak": max_static_streak,
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
    product_ready: bool = False,
    visual_style_id: str = "clean-narration",
    auto_regenerate: bool = False,
    requested_count: int | None = None,
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
    complete_checkpoint = (
        not demo
        and all((scenes_dir / f"scene-{index:03}.jpg").is_file() for index in range(1, count + 1))
    )
    if complete_checkpoint:
        # A restarted job with a complete image checkpoint should never block
        # on the visual-director API again. Reconstruct deterministic briefs,
        # inspect the saved images, and finalize the manifest immediately.
        briefs = scene_briefs(
            script_text,
            count,
            book_title=book_title,
            visual_style_id=visual_style_id,
        )
        director_prompt = {
            "version": "checkpoint-recovery-v1",
            "source": "all portrait images already existed; deterministic briefs reconstructed",
        }
    else:
        briefs, director_prompt = direct_scene_briefs(
            script_text,
            count=count,
            book_title=book_title,
            book_author=book_author,
            selling_points=selling_points,
            cover_available=cover_available,
            settings=settings,
            product_ready=product_ready,
            visual_style_id=visual_style_id,
            demo=demo,
        )
    scenes: list[Path] = []
    prompt_snapshots: list[dict[str, str | int]] = []
    quality: list[dict[str, object]] = []
    hashes: list[int] = []
    regeneration_count = 0
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
                selling_points=selling_points,
                visual_style_id=visual_style_id,
                product_ready=product_ready,
            )
            prompt_snapshots.append({"scene_index": index, **snapshot})
            scene = scenes_dir / f"scene-{index:03}.jpg"
            reused = _reuse_existing_scene(scene)
            report: dict[str, object] = {}
            generation_fallback = ""
            max_attempts = 1 + (IMAGE_QUALITY_REGENERATIONS if auto_regenerate else 0)
            for attempt in range(max_attempts):
                if not reused or attempt > 0:
                    try:
                        _generate_image_api(
                            prompt,
                            scene,
                            settings,
                            size=request_size,
                        )
                    except RuntimeError as exc:
                        can_use_closing_fallback = (
                            "IMAGE_GENERATION_TRANSIENT_ERROR" in str(exc)
                            and index == count
                            and len(scenes) == count - 1
                            and bool(cover_path and cover_path.is_file())
                        )
                        if not can_use_closing_fallback:
                            raise
                        compose_cover_closing_fallback(cover_path, scene)
                        generation_fallback = "ai-cover-closing-composite"
                    if attempt > 0:
                        regeneration_count += 1
                # Normalize at the orchestration boundary as well as in the
                # built-in provider adapter. This keeps downstream video layout
                # deterministic when a compatible provider returns a different
                # aspect ratio or a custom adapter writes its native dimensions.
                with Image.open(scene) as generated:
                    normalized = _cover(generated, SCENE_SIZE)
                normalized.save(scene, quality=94)
                report = inspect_scene_image(
                    scene,
                    previous_hashes=hashes,
                    visual_style_id=visual_style_id,
                )
                retryable_flags = {
                    "near_blank",
                    "near_duplicate",
                    "low_commercial_contrast",
                    "low_color_energy",
                }
                if generation_fallback:
                    break
                if not retryable_flags.intersection(report["flags"]):
                    break
                reused = False
            report["generation_attempts"] = attempt + 1
            if generation_fallback:
                report["generation_fallback"] = generation_fallback
            scenes.append(scene)
            current_hash = int(str(report["average_hash"]), 16)
            hashes.append(current_hash)
            quality.append({"scene_index": index, **report})

    if demo:
        for index, scene in enumerate(scenes, start=1):
            report = inspect_scene_image(
                scene,
                previous_hashes=hashes,
                visual_style_id=visual_style_id,
            )
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
    plan_audit = audit_scene_plan(
        briefs,
        cover_available=cover_available,
        product_ready=product_ready,
        visual_style_id=visual_style_id,
    )
    quality_status = (
        "passed"
        if not flagged and plan_audit["status"] == "passed"
        else "needs_review"
    )
    manifest = write_json(
        root / "manifest.json",
        {
            "schema_version": 2,
            "count": count,
            "requested_count": requested_count if requested_count is not None else count,
            "count_strategy": "automatic-duration" if requested_count == 0 else "manual",
            "mode": "demo-reference-portrait" if demo else "api-portrait-generation",
            "request_size": request_size,
            "output_size": f"{SCENE_SIZE[0]}x{SCENE_SIZE[1]}",
            "grid_count": len(contact_sheets),
            "contact_sheet_count": len(contact_sheets),
            "prompt_version": IMAGE_PROMPT_VERSION,
            "prompt_source": PROMPT_SOURCE,
            "style_bible": STYLE_BIBLE,
            "visual_style_id": visual_style_id,
            "product_ready": product_ready,
            "cover_available": cover_available,
            "director_prompt": director_prompt,
            "prompt_snapshots": prompt_snapshots,
            "briefs": briefs,
            # Keep the old key as a compatibility alias for the review UI.
            "grids": [str(path.resolve()) for path in contact_sheets],
            "contact_sheets": [str(path.resolve()) for path in contact_sheets],
            "scenes": [str(path.resolve()) for path in scenes],
            "quality": {
                "status": quality_status,
                "flagged_count": len(flagged),
                "regeneration_count": regeneration_count,
                "checks": quality,
                "plan_audit": plan_audit,
            },
        },
    )
    return manifest, contact_sheets, scenes

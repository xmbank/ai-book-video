from __future__ import annotations

import html
import json
import shutil
from pathlib import Path

from book_video_workbench.config import Settings
from book_video_workbench.util import read_json, require_command, run_command, write_json


def _copy_asset(source: Path, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target.name


def _headline_html(value: str) -> str:
    escaped = html.escape(value)
    if len(value) > 8:
        for punctuation in ("，", "。", "！", "？", "；", "："):
            if punctuation in value[:-2]:
                return escaped.replace(
                    punctuation,
                    punctuation + '<span class="headline-break">',
                    1,
                ) + "</span>"
    return escaped


def generate_project(
    manifest_path: Path,
    task_dir: Path,
    settings: Settings,
    *,
    project_name: str = "render-project",
) -> Path:
    manifest = read_json(manifest_path)
    subtitles = read_json(Path(manifest["subtitles"]["path"]))["items"]
    project_dir = task_dir / project_name
    assets_dir = project_dir / "assets"
    if project_dir.exists():
        shutil.rmtree(project_dir)
    assets_dir.mkdir(parents=True, exist_ok=True)

    narration_name = _copy_asset(Path(manifest["audio"]["path"]), assets_dir / "narration.wav")
    gsap_source = settings.project_root / "node_modules" / "gsap" / "dist" / "gsap.min.js"
    if not gsap_source.is_file():
        raise RuntimeError("缺少前端渲染依赖；请在项目目录执行 npm install")
    _copy_asset(gsap_source, assets_dir / "gsap.min.js")

    source_video_name = None
    source_video = manifest["assets"].get("source_video")
    if source_video:
        source_video_name = _copy_asset(Path(source_video), assets_dir / "source.mp4")
    cover_name = None
    book_cover = manifest["assets"].get("book_cover")
    if book_cover:
        cover_name = _copy_asset(Path(book_cover), assets_dir / ("cover" + Path(book_cover).suffix.lower()))

    scene_image_names: list[str] = []
    for index, source in enumerate(manifest["assets"].get("scene_images") or [], start=1):
        scene_image_names.append(
            _copy_asset(Path(source), assets_dir / f"scene-{index:03}{Path(source).suffix.lower()}")
        )

    duration = float(manifest["duration_seconds"])
    selling_points = [
        html.escape(str(item))
        for item in manifest.get("content", {}).get("selling_points") or []
        if str(item).strip()
    ]
    scenes_html = []
    scene_animations = []
    for scene in manifest["scenes"]:
        scene_id = html.escape(scene["id"], quote=True)
        scene_start = float(scene["start"])
        scene_duration = float(scene["duration"])
        scene_index = len(scenes_html)
        media = ""
        if scene_image_names:
            image_name = scene_image_names[(len(scenes_html)) % len(scene_image_names)]
            media = f'<img id="{scene_id}-image" class="scene-image" data-layout-allow-overflow src="assets/{image_name}" alt="">'
        elif source_video_name:
            media = (
                f'<video id="{scene_id}-source-video" class="background-video" src="assets/{source_video_name}" muted '
                'playsinline preload="auto"></video><div class="media-shade"></div>'
            )
        cover = ""
        cover_roles = {"product_space", "product_reveal", "product_detail", "closing"}
        product_scene = bool(
            cover_name
            and (
                scene.get("shot_role") in cover_roles
                or len(scenes_html) == len(manifest["scenes"]) - 1
            )
        )
        if product_scene:
            cover_suffix = Path(cover_name).suffix.lower()
            scene_cover_name = f"cover-scene-{scene_index + 1:03}{cover_suffix}"
            shutil.copy2(assets_dir / cover_name, assets_dir / scene_cover_name)
            chips = "".join(
                f'<span>{point}</span>' for point in selling_points[:3]
            )
            cover = (
                '<div class="product-stage">'
                f'<img class="book-cover" src="assets/{html.escape(scene_cover_name)}" alt="">'
                '<div class="product-copy"><small>THIS BOOK / 本期好书</small>'
                f'<strong>《{html.escape(manifest["content"]["book_title"])}》</strong>'
                f'<div class="selling-point-chips">{chips}</div></div></div>'
            )
        direction_from = -2 if scene_index % 2 == 0 else 2
        direction_to = 2 if scene_index % 2 == 0 else -2
        animation = (
            f'timeline.fromTo("#{scene_id}", {{ opacity: 0 }}, '
            f'{{ opacity: 1, duration: 0.22, ease: "power2.out" }}, {scene_start}); '
            f'timeline.fromTo("#{scene_id} .scene-image", '
            f'{{ scale: 1.12, xPercent: {direction_from} }}, '
            f'{{ scale: 1.01, xPercent: {direction_to}, duration: {max(2.0, scene_duration):.3f}, ease: "none" }}, '
            f'{scene_start}); '
        )
        if product_scene:
            animation += (
                f'timeline.fromTo("#{scene_id} .product-stage", '
                '{ opacity: 0, y: 40, scale: .94 }, '
                '{ opacity: 1, y: 0, scale: 1, duration: .55, ease: "power2.out" }, '
                f'{scene_start + 0.08}); '
            )
        scene_animations.append(animation)
        headline_size = 66 if len(scene["headline"]) > 8 else 74
        headline_class = (
            " headline-key"
            if len(scenes_html) in {0, len(manifest["scenes"]) - 1} or product_scene
            else " headline-hidden"
        )
        scenes_html.append(
            f'''<section id="{scene_id}" class="clip scene{' product-scene' if product_scene else ''}" data-role="{html.escape(str(scene.get('shot_role') or ''), quote=True)}" data-start="{scene['start']}" data-duration="{scene['duration']}" data-track-index="1" style="--bg:{scene['background']};--accent:{scene['accent']};--fg:{scene['foreground']};--headline-size:{headline_size}px">
  {media}
  <div class="headline{headline_class}">{_headline_html(scene['headline'])}</div>
  {cover}
</section>'''
        )

    captions_html = []
    for index, item in enumerate(subtitles, start=1):
        captions_html.append(
            f'''<div id="caption-{index}" class="clip caption" data-start="{item['start']}" data-duration="{round(float(item['end']) - float(item['start']), 3)}" data-track-index="8"><span>{html.escape(item['text'])}</span></div>'''
        )
    title = html.escape(manifest["content"]["book_title"])
    author = html.escape(manifest["content"].get("book_author") or "")
    declaration = html.escape(manifest["content"].get("declaration") or "")
    style_id = html.escape(manifest["template"]["id"], quote=True)
    html_text = f'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=1080,height=1920">
  <script src="assets/gsap.min.js"></script>
  <style>
    @font-face {{ font-family: "Workbench Chinese"; src: local("PingFang SC"); }}
    @font-face {{ font-family: "Workbench Chinese Fallback"; src: local("Hiragino Sans GB"); }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; width: 1080px; height: 1920px; overflow: hidden; background: #101216; }}
    body {{ font-family: "Workbench Chinese", "Workbench Chinese Fallback", sans-serif; color: #fff; letter-spacing: 0; }}
    #root {{ position: relative; width: 1080px; height: 1920px; overflow: hidden; }}
    .scene {{ position: absolute; inset: 0; overflow: hidden; background: var(--bg); }}
    .scene::before {{ content: ""; position: absolute; z-index: 2; inset: 0; background: rgba(7, 10, 12, .07); }}
    .scene::after {{ content: ""; position: absolute; z-index: 3; inset: auto 0 0; height: 520px; background: linear-gradient(180deg, rgba(5,7,9,0), rgba(5,7,9,.48) 48%, rgba(5,7,9,.72)); }}
    .scene-image {{ position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }}
    .background-video {{ position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }}
    .media-shade {{ position: absolute; inset: 0; background: rgba(10, 12, 15, .6); }}
    .headline {{ position: absolute; z-index: 4; left: 74px; right: 74px; top: 235px; color: white; font-size: var(--headline-size); line-height: 1.18; font-weight: 800; overflow-wrap: anywhere; text-wrap: balance; text-shadow: 0 4px 16px rgba(0,0,0,.72); }}
    .headline-break {{ display: block; }}
    .headline-hidden {{ display: none; }}
    .product-stage {{ position: absolute; z-index: 6; left: 72px; right: 72px; top: 560px; min-height: 650px; display: grid; grid-template-columns: 420px 1fr; align-items: center; gap: 46px; padding: 44px; border: 1px solid rgba(255,255,255,.22); border-radius: 34px; background: linear-gradient(135deg, rgba(13,16,18,.82), rgba(13,16,18,.48)); box-shadow: 0 30px 90px rgba(0,0,0,.3); backdrop-filter: blur(14px); }}
    .book-cover {{ width: 100%; max-height: 570px; object-fit: contain; filter: drop-shadow(0 24px 28px rgba(0,0,0,.42)); }}
    .product-copy {{ display: flex; flex-direction: column; gap: 20px; min-width: 0; }}
    .product-copy small {{ font-size: 22px; font-weight: 800; letter-spacing: .08em; color: #ffd25f; }}
    .product-copy strong {{ font-size: 48px; line-height: 1.2; color: white; }}
    .selling-point-chips {{ display: flex; flex-direction: column; gap: 14px; }}
    .selling-point-chips span {{ padding: 14px 18px; border-left: 7px solid #ffd25f; border-radius: 8px 18px 18px 8px; background: rgba(255,255,255,.12); font-size: 27px; line-height: 1.3; font-weight: 700; color: white; }}
    .caption {{ position: absolute; z-index: 10; left: 62px; right: 62px; bottom: 245px; min-height: 170px; display: flex; align-items: center; justify-content: center; text-align: center; padding: 24px 30px; font-size: 58px; line-height: 1.26; font-weight: 800; text-shadow: 0 3px 8px rgba(0,0,0,.9); }}
    .caption span {{ padding: 8px 14px; background: rgba(0,0,0,.36); border-radius: 10px; box-decoration-break: clone; -webkit-box-decoration-break: clone; }}
    .brand {{ position: absolute; z-index: 20; left: 62px; right: 62px; bottom: 122px; font-size: 23px; color: rgba(255,255,255,.9); text-shadow: 0 2px 6px rgba(0,0,0,.75); }}
    .declaration {{ position: absolute; z-index: 20; left: 62px; right: 62px; bottom: 68px; font-size: 18px; color: rgba(255,255,255,.72); }}
    body.typewriter-dark .scene::before {{ background: rgba(0,0,0,.68); }}
    body.typewriter-dark .scene-image {{ filter: grayscale(.55) contrast(1.08); }}
    body.typewriter-dark .headline {{ top: 420px; font-family: ui-monospace, monospace; font-size: 68px; }}
    body.dark-knowledge .scene::before {{ background: rgba(5,17,24,.52); }}
    body.dark-knowledge .headline {{ padding: 32px; background: rgba(5,12,16,.72); border-left: 12px solid #74c9bd; }}
    body.book-broadcast .headline {{ top: 185px; padding: 22px 26px; background: rgba(0,0,0,.72); font-size: 66px; }}
    body.book-sales .scene::before {{ background: linear-gradient(180deg, rgba(0,0,0,.16), rgba(0,0,0,0) 38%); }}
    body.book-sales .scene-image {{ filter: saturate(1.12) contrast(1.06); }}
    body.book-sales .headline {{ top: 185px; max-width: 900px; padding: 22px 28px; border-left: 12px solid #f2c94c; background: linear-gradient(90deg, rgba(0,0,0,.76), rgba(0,0,0,.18)); font-size: 64px; }}
    body.book-sales .product-scene .headline {{ top: 185px; right: 240px; }}
    body.book-sales .caption span {{ background: rgba(0,0,0,.28); text-shadow: 0 3px 10px rgba(0,0,0,.95), 0 0 2px #000; }}
  </style>
</head>
<body class="{style_id}">
  <main id="root" data-composition-id="main" data-start="0" data-duration="{duration}" data-width="1080" data-height="1920" data-fps="30">
    {''.join(scenes_html)}
    <audio id="narration" class="clip" src="assets/{narration_name}" data-start="0" data-duration="{duration}" data-track-index="5" data-volume="1" preload="auto"></audio>
    {''.join(captions_html)}
    <div class="brand">《{title}》{(' · ' + author) if author else ''}</div>
    <div class="declaration">{declaration}</div>
  </main>
  <script>
    window.__timelines = window.__timelines || {{}};
    const timeline = gsap.timeline({{ paused: true }});
    {''.join(scene_animations)}
    window.__timelines["main"] = timeline;
  </script>
</body>
</html>
'''
    index_path = project_dir / "index.html"
    index_path.write_text(html_text, encoding="utf-8")
    write_json(
        project_dir / "meta.json",
        {"id": "book-sales-main", "name": manifest["content"]["book_title"]},
    )
    write_json(
        project_dir / "hyperframes.json",
        {
            "$schema": "https://hyperframes.heygen.com/schema/hyperframes.json",
            "paths": {"assets": "assets"},
        },
    )
    return project_dir


def render_project(project_dir: Path, output_path: Path, settings: Settings) -> Path:
    cli = settings.project_root / "node_modules" / ".bin" / "hyperframes"
    if not cli.is_file():
        raise RuntimeError("缺少 HyperFrames；请在项目目录执行 npm install")
    check_log = project_dir.parent / "logs" / "hyperframes-check.log"
    check = run_command(
        [str(cli), "check", str(project_dir), "--json"],
        cwd=settings.project_root,
        log_path=check_log,
        timeout=120,
    )
    check_result = json.loads(check.stdout)
    if not check_result.get("ok"):
        raise RuntimeError(f"HyperFrames 模板校验失败，详见 {check_log}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            str(cli),
            "render",
            str(project_dir),
            "--output",
            str(output_path),
            "--fps",
            "30",
            "--quality",
            "standard",
            "--workers",
            "4",
            "--strict",
        ],
        cwd=settings.project_root,
        log_path=project_dir.parent / "logs" / "hyperframes-render.log",
        timeout=1800,
    )
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError("HyperFrames 返回成功但没有生成有效 MP4")
    return output_path


def validate_video(video_path: Path, report_path: Path) -> Path:
    ffprobe = require_command("ffprobe")
    proc = run_command(
        [
            ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(video_path),
        ]
    )
    probe = json.loads(proc.stdout)
    streams = probe.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    failures = []
    if not video:
        failures.append("缺少视频轨")
    else:
        if (video.get("width"), video.get("height")) != (1080, 1920):
            failures.append(f"分辨率不是 1080x1920: {video.get('width')}x{video.get('height')}")
        if video.get("codec_name") != "h264":
            failures.append(f"视频编码不是 H.264: {video.get('codec_name')}")
    if not audio:
        failures.append("缺少音频轨")
    elif audio.get("codec_name") != "aac":
        failures.append(f"音频编码不是 AAC: {audio.get('codec_name')}")
    duration = float((probe.get("format") or {}).get("duration") or 0)
    if duration <= 0:
        failures.append("成片时长无效")
    report = {
        "schema_version": 1,
        "valid": not failures,
        "failures": failures,
        "summary": {
            "duration_seconds": duration,
            "size_bytes": video_path.stat().st_size,
            "video_codec": video.get("codec_name") if video else None,
            "audio_codec": audio.get("codec_name") if audio else None,
            "width": video.get("width") if video else None,
            "height": video.get("height") if video else None,
            "frame_rate": video.get("avg_frame_rate") if video else None,
        },
        "ffprobe": probe,
    }
    write_json(report_path, report)
    if failures:
        raise RuntimeError("成片验收失败: " + "; ".join(failures))
    return report_path

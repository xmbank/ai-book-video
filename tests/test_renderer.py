import json
from pathlib import Path

from PIL import Image

from book_video_workbench.config import Settings
from book_video_workbench.renderer import _headline_html, generate_project
from book_video_workbench.storyboard import build_storyboard


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        capture_backend_dir=tmp_path / "capture",
        llm_base_url="https://example.com/v1",
        llm_api_key="",
        llm_model="",
        image_base_url="https://example.com/v1",
        image_api_key="",
        image_model="gpt-image-2",
        image_size="1024x1536",
        volc_tts_endpoint="https://example.com/tts",
        volc_tts_api_key="",
        volc_tts_resource_id="seed-tts-2.0",
        volc_tts_voice_type="",
        reference_demo_video=tmp_path / "reference.mp4",
    )


def test_long_headline_breaks_at_punctuation() -> None:
    assert _headline_html("把一件小事，长期做对") == (
        '把一件小事，<span class="headline-break">长期做对</span>'
    )


def test_short_headline_stays_on_one_line() -> None:
    assert _headline_html("一本好书") == "一本好书"


def test_sales_renderer_builds_product_hero_and_only_animates_existing_cards(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    gsap = tmp_path / "node_modules" / "gsap" / "dist" / "gsap.min.js"
    gsap.parent.mkdir(parents=True)
    gsap.write_text("window.gsap = {};", encoding="utf-8")
    subtitle = tmp_path / "subtitles.json"
    subtitle.write_text(
        json.dumps(
            {
                "items": [
                    {"start": 0, "end": 4, "text": "先看你每天真正做了什么"},
                    {"start": 4, "end": 8, "text": "再决定这本书是否适合你"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    audio = tmp_path / "narration.wav"
    audio.write_bytes(b"wav")
    cover = tmp_path / "cover.jpg"
    Image.new("RGB", (600, 900), (195, 45, 35)).save(cover)
    scenes = []
    for index, color in enumerate(((35, 85, 145), (205, 150, 35), (55, 135, 90)), 1):
        scene = tmp_path / f"scene-{index}.jpg"
        Image.new("RGB", (720, 1280), color).save(scene)
        scenes.append(scene)
    _, manifest = build_storyboard(
        tmp_path,
        title="抗老生活内容",
        book_title="抗老生活",
        book_author="测试作者",
        hook="先看习惯，再谈抗老",
        duration=12,
        subtitle_path=subtitle,
        tts_path=audio,
        source_video=None,
        book_cover=cover,
        scene_images=scenes,
        scene_briefs=[
            {"shot_role": "pattern_interrupt"},
            {"shot_role": "product_reveal"},
            {"shot_role": "closing"},
        ],
        style_id="book-sales",
        selling_points=["关注日常习惯", "提供生活方式观察"],
    )

    project = generate_project(manifest, tmp_path, settings)
    html = (project / "index.html").read_text(encoding="utf-8")

    assert '<body class="book-sales">' in html
    assert html.count('class="product-stage"') == 2
    assert "关注日常习惯" in html
    assert "提供生活方式观察" in html
    assert 'timeline.fromTo("#scene-1 .product-stage"' not in html
    assert 'timeline.fromTo("#scene-2 .product-stage"' in html
    assert 'timeline.fromTo("#scene-3 .product-stage"' in html
    assert "linear-gradient(180deg, rgba(5,7,9,0)" in html
    assert "<br>" not in html
    assert 'class="scene-mark"' not in html
    assert 'class="page-number"' not in html
    assert ".caption { position: absolute;" in html
    assert "font-size: 58px; line-height: 1.26" in html

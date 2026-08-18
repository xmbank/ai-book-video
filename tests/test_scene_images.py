from pathlib import Path
import base64
import io
import json
import urllib.error

from PIL import Image, ImageDraw

from book_video_workbench.article_prompts import (
    IMAGE_PROMPT_VERSION,
    IMAGE_SYSTEM_PROMPT,
    build_image_prompt,
)
from book_video_workbench.config import Settings
import book_video_workbench.scene_images as scene_images
from book_video_workbench.scene_images import (
    audit_scene_plan,
    compose_cover_closing_fallback,
    compose_grid,
    generate_ai_book_cover,
    generate_scene_images,
    inspect_scene_image,
    split_grid,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        capture_backend_dir=tmp_path / "capture",
        llm_base_url="https://example.com/v1",
        llm_api_key="",
        llm_model="",
        image_base_url="https://example.com/v1",
        image_api_key="test",
        image_model="gpt-image-2",
        image_size="1536x1024",
        volc_tts_endpoint="https://example.com/tts",
        volc_tts_api_key="",
        volc_tts_resource_id="seed-tts-2.0",
        volc_tts_voice_type="",
        reference_demo_video=tmp_path / "reference.mp4",
    )


def _briefs(count: int) -> tuple[list[dict], dict]:
    return (
        [
            {
                "id": index,
                "script_text": f"第{index}句。",
                "narration": f"第{index}句。",
                "shot_role": "detail" if index % 2 else "editorial_symbol",
                "visual_purpose": f"第{index}个不同的叙事目的",
                "subject": f"主体{index}",
                "action": f"动作{index}",
                "location": "阅读空间",
                "framing": "9:16 竖屏",
                "lighting": "自然光",
                "continuity": "同一材质",
                "avoid": "文字和假封面",
                "visual_brief": f"第{index}个不同视觉描述",
                "safety_translation": "不要画医疗病理。",
            }
            for index in range(1, count + 1)
        ],
        {"version": "test-director"},
    )


def test_contact_sheet_round_trip_keeps_portrait_cells(tmp_path: Path) -> None:
    colors = [(index * 20, 80, 180 - index * 10) for index in range(9)]
    sources = []
    for index, color in enumerate(colors):
        path = tmp_path / f"source-{index}.jpg"
        Image.new("RGB", (720, 1280), color).save(path)
        sources.append(path)
    grid = compose_grid(sources, tmp_path / "contact.jpg")
    with Image.open(grid) as image:
        assert image.size == (720, 1281)
    scenes = split_grid(grid, tmp_path / "scenes", start_index=1)
    assert len(scenes) == 9
    with Image.open(scenes[0]) as image:
        assert image.size == (720, 1280)


def test_real_generation_creates_individual_portrait_scenes(tmp_path: Path, monkeypatch) -> None:
    captured = []

    def fake_director(*args, **kwargs):
        return _briefs(kwargs["count"])

    def fake_generate(prompt, output_path, settings, *, size):
        captured.append((prompt, size))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        color = (40 + len(captured) * 10, 100, 160)
        Image.new("RGB", (1024, 1536), color).save(output_path)
        return output_path

    monkeypatch.setattr(scene_images, "direct_scene_briefs", fake_director)
    monkeypatch.setattr(scene_images, "_generate_image_api", fake_generate)
    manifest_path, contacts, scenes = generate_scene_images(
        "第一句。第二句。第三句。第四句。第五句。第六句。",
        count=6,
        task_dir=tmp_path,
        version=1,
        settings=_settings(tmp_path),
        demo=False,
        book_title="测试书",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(captured) == 6
    assert len(contacts) == 1
    assert len(scenes) == 6
    assert IMAGE_SYSTEM_PROMPT in captured[0][0]
    assert captured[0][1] == "1024x1536"
    assert "请生成这一张原生 9:16 竖屏场景图" in captured[0][0]
    assert "不生成九宫格" in captured[0][0]
    assert manifest["mode"] == "api-portrait-generation"
    assert manifest["prompt_version"] == IMAGE_PROMPT_VERSION
    assert manifest["contact_sheet_count"] == 1
    assert all(Image.open(path).size == (720, 1280) for path in scenes)


def test_real_generation_reuses_valid_checkpoint_scenes(tmp_path: Path, monkeypatch) -> None:
    generated = []

    def fake_director(*args, **kwargs):
        return _briefs(kwargs["count"])

    def fake_generate(prompt, output_path, settings, *, size):
        generated.append(output_path.name)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1024, 1536), (50, 110, 170)).save(output_path)
        return output_path

    scenes_dir = tmp_path / "scene-images" / "v1" / "scenes"
    scenes_dir.mkdir(parents=True)
    Image.new("RGB", (720, 1280), (120, 80, 40)).save(scenes_dir / "scene-001.jpg")
    Image.new("RGB", (720, 1280), (80, 120, 40)).save(scenes_dir / "scene-002.jpg")

    monkeypatch.setattr(scene_images, "direct_scene_briefs", fake_director)
    monkeypatch.setattr(scene_images, "_generate_image_api", fake_generate)
    _, _, scenes = generate_scene_images(
        "第一句。第二句。第三句。第四句。第五句。第六句。",
        count=6,
        task_dir=tmp_path,
        version=1,
        settings=_settings(tmp_path),
        demo=False,
    )

    assert generated == [
        "scene-003.jpg",
        "scene-004.jpg",
        "scene-005.jpg",
        "scene-006.jpg",
    ]
    assert len(scenes) == 6


def test_image_api_retries_transient_disconnects(tmp_path: Path, monkeypatch) -> None:
    image_buffer = io.BytesIO()
    Image.new("RGB", (1024, 1536), (40, 100, 160)).save(image_buffer, format="PNG")
    body = json.dumps(
        {"data": [{"b64_json": base64.b64encode(image_buffer.getvalue()).decode()}]}
    ).encode()
    attempts = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return body

    def fake_urlopen(request, timeout):
        attempts.append(timeout)
        if len(attempts) < 3:
            raise urllib.error.URLError("temporary TLS disconnect")
        return FakeResponse()

    monkeypatch.setattr(scene_images.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(scene_images.time, "sleep", lambda _: None)
    output = tmp_path / "scene.jpg"
    scene_images._generate_image_api(
        "测试提示词",
        output,
        _settings(tmp_path),
        size="1024x1536",
    )

    assert len(attempts) == 3
    assert output.is_file()
    assert Image.open(output).size == (720, 1280)


def test_ai_book_cover_uses_generated_artwork_and_local_exact_title(
    tmp_path: Path, monkeypatch
) -> None:
    image_buffer = io.BytesIO()
    Image.new("RGB", (1024, 1536), (42, 112, 124)).save(image_buffer, format="PNG")
    prompts: list[tuple[str, str]] = []

    def fake_request(prompt, settings, *, size):
        prompts.append((prompt, size))
        return image_buffer.getvalue()

    monkeypatch.setattr(scene_images, "_request_generated_image", fake_request)
    output = tmp_path / "book" / "assets" / "ai-cover-v1.jpg"
    metadata = output.with_suffix(".json")
    cover, saved_metadata = generate_ai_book_cover(
        book_title="身体重置",
        book_author="测试作者",
        selling_points=["每餐蛋白质配速", "彩虹饮食"],
        output_path=output,
        metadata_path=metadata,
        settings=_settings(tmp_path),
    )

    assert cover == output
    assert saved_metadata == metadata
    assert Image.open(output).size == (900, 1350)
    value = json.loads(metadata.read_text(encoding="utf-8"))
    assert value["book_title"] == "身体重置"
    assert value["text_rendering"] == "deterministic-local-overlay"
    assert prompts[0][1] == "1024x1536"
    assert "Do not draw any words" in prompts[0][0]


def test_cover_closing_fallback_builds_portrait_scene(tmp_path: Path) -> None:
    cover = tmp_path / "ai-cover.jpg"
    Image.new("RGB", (900, 1350), (42, 112, 124)).save(cover)
    output = compose_cover_closing_fallback(cover, tmp_path / "scene-019.jpg")

    assert output.is_file()
    assert Image.open(output).size == (720, 1280)


def test_transient_failure_on_final_scene_reuses_ai_cover(
    tmp_path: Path, monkeypatch
) -> None:
    def fake_director(*args, **kwargs):
        briefs, prompt = _briefs(kwargs["count"])
        briefs[-1]["shot_role"] = "closing"
        return briefs, prompt

    def fake_generate(prompt, output_path, settings, *, size):
        index = int(output_path.stem.split("-")[-1])
        if index == 6:
            raise RuntimeError("IMAGE_GENERATION_TRANSIENT_ERROR: upstream timeout")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1024, 1536), (40 + index * 20, 90, 150)).save(output_path)
        return output_path

    cover = tmp_path / "ai-cover.jpg"
    Image.new("RGB", (900, 1350), (42, 112, 124)).save(cover)
    monkeypatch.setattr(scene_images, "direct_scene_briefs", fake_director)
    monkeypatch.setattr(scene_images, "_generate_image_api", fake_generate)
    manifest_path, _, scenes = generate_scene_images(
        "第一句。第二句。第三句。第四句。第五句。第六句。",
        count=6,
        task_dir=tmp_path,
        version=1,
        settings=_settings(tmp_path),
        demo=False,
        book_title="测试书",
        selling_points=["卖点一", "卖点二"],
        book_cover=str(cover),
        product_ready=True,
        visual_style_id="book-sales",
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(scenes) == 6
    assert scenes[-1].is_file()
    assert manifest["quality"]["checks"][-1]["generation_fallback"] == "ai-cover-closing-composite"


def test_complete_scene_checkpoint_skips_director_and_image_apis(
    tmp_path: Path, monkeypatch
) -> None:
    scenes_dir = tmp_path / "scene-images" / "v1" / "scenes"
    scenes_dir.mkdir(parents=True)
    for index in range(1, 7):
        Image.new("RGB", (720, 1280), (30 + index * 25, 80, 150)).save(
            scenes_dir / f"scene-{index:03}.jpg"
        )

    def unexpected(*args, **kwargs):
        raise AssertionError("complete checkpoint must not call an external provider")

    monkeypatch.setattr(scene_images, "direct_scene_briefs", unexpected)
    monkeypatch.setattr(scene_images, "_generate_image_api", unexpected)
    manifest_path, contacts, scenes = generate_scene_images(
        "第一句。第二句。第三句。第四句。第五句。第六句。",
        count=6,
        task_dir=tmp_path,
        version=1,
        settings=_settings(tmp_path),
        demo=False,
        book_title="测试书",
        selling_points=["卖点一", "卖点二"],
        product_ready=True,
        visual_style_id="book-sales",
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(scenes) == 6
    assert len(contacts) == 1
    assert manifest["director_prompt"]["version"] == "checkpoint-recovery-v1"


def test_image_quality_flags_near_duplicates(tmp_path: Path) -> None:
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    Image.new("RGB", (720, 1280), (120, 100, 80)).save(first)
    Image.new("RGB", (720, 1280), (120, 100, 80)).save(second)
    first_report = inspect_scene_image(first)
    report = inspect_scene_image(
        second,
        previous_hashes=[int(str(first_report["average_hash"]), 16)],
    )
    assert "near_duplicate" in report["flags"]


def test_sales_image_prompt_includes_product_truth_and_commercial_direction() -> None:
    prompt, _ = build_image_prompt(
        {
            "shot_role": "product_reveal",
            "visual_purpose": "建立购买理由",
            "subject": "真实封面后期合成区",
            "action": "人物视线指向商品区",
        },
        shot_index=2,
        shot_count=12,
        book_title="抗老生活",
        cover_available=True,
        selling_points=["从日常习惯理解健康老化", "提供可读的生活观察"],
        visual_style_id="book-sales",
        product_ready=True,
    )

    assert "强转化图书广告" in prompt
    assert "从日常习惯理解健康老化" in prompt
    assert "不要生成空白书" in prompt
    assert "增强停留、购买理由或理想状态" in prompt


def test_sales_scene_plan_audit_checks_product_people_and_blank_books() -> None:
    briefs, _ = _briefs(12)
    roles = [
        "pattern_interrupt",
        "product_reveal",
        "pain_point",
        "human_action",
        "method_demo",
        "detail",
        "product_space",
        "proof",
        "desired_outcome",
        "transition",
        "human_action",
        "closing",
    ]
    for brief, role in zip(briefs, roles):
        brief["shot_role"] = role

    report = audit_scene_plan(
        briefs,
        cover_available=True,
        product_ready=True,
        visual_style_id="book-sales",
    )
    assert report["status"] == "passed"
    assert report["product_scene_count"] == 3
    assert report["human_emotion_scene_count"] >= 4

    briefs[3]["subject"] = "一本空白书"
    failed = audit_scene_plan(
        briefs,
        cover_available=True,
        product_ready=True,
        visual_style_id="book-sales",
    )
    assert "blank_book_placeholder" in failed["failures"]


def test_sales_generation_regenerates_low_energy_images_once(
    tmp_path: Path, monkeypatch
) -> None:
    calls: dict[str, int] = {}

    def fake_director(*args, **kwargs):
        briefs, prompt = _briefs(kwargs["count"])
        roles = [
            "pattern_interrupt",
            "product_reveal",
            "pain_point",
            "human_action",
            "method_demo",
            "closing",
        ]
        for brief, role in zip(briefs, roles):
            brief["shot_role"] = role
        return briefs, prompt

    def fake_generate(prompt, output_path, settings, *, size):
        calls[output_path.name] = calls.get(output_path.name, 0) + 1
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if calls[output_path.name] == 1:
            image = Image.new("RGB", (1024, 1536), (120, 120, 120))
        else:
            image = Image.new("RGB", (1024, 1536), (235, 174, 35))
            draw = ImageDraw.Draw(image)
            offset = int(output_path.stem.split("-")[-1]) * 41
            draw.rectangle((80 + offset, 100, 600 + offset, 1000), fill=(28, 72, 155))
        image.save(output_path)
        return output_path

    cover = tmp_path / "cover.jpg"
    Image.new("RGB", (600, 900), (200, 40, 30)).save(cover)
    monkeypatch.setattr(scene_images, "direct_scene_briefs", fake_director)
    monkeypatch.setattr(scene_images, "_generate_image_api", fake_generate)
    manifest_path, _, _ = generate_scene_images(
        "第一句。第二句。第三句。第四句。第五句。第六句。",
        count=6,
        task_dir=tmp_path,
        version=1,
        settings=_settings(tmp_path),
        demo=False,
        book_title="测试书",
        selling_points=["卖点一", "卖点二"],
        book_cover=str(cover),
        product_ready=True,
        visual_style_id="book-sales",
        auto_regenerate=True,
        requested_count=0,
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert all(attempts == 2 for attempts in calls.values())
    assert manifest["count_strategy"] == "automatic-duration"
    assert manifest["quality"]["regeneration_count"] == 6
    assert all(
        item["generation_attempts"] == 2
        for item in manifest["quality"]["checks"]
    )

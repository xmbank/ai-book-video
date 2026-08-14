from pathlib import Path
import json

from PIL import Image

from book_video_workbench.article_prompts import (
    IMAGE_PROMPT_VERSION,
    IMAGE_SYSTEM_PROMPT,
)
from book_video_workbench.config import Settings
import book_video_workbench.scene_images as scene_images
from book_video_workbench.scene_images import compose_grid, generate_scene_images, split_grid


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


def test_grid_round_trip_creates_nine_portrait_scenes(tmp_path: Path) -> None:
    colors = [(index * 20, 80, 180 - index * 10) for index in range(9)]
    sources = []
    for index, color in enumerate(colors):
        path = tmp_path / f"source-{index}.jpg"
        Image.new("RGB", (640, 360), color).save(path)
        sources.append(path)
    grid = compose_grid(sources, tmp_path / "grid.jpg")
    with Image.open(grid) as image:
        assert image.size == (1536, 864)
    scenes = split_grid(grid, tmp_path / "scenes", start_index=1)
    assert len(scenes) == 9
    with Image.open(scenes[0]) as image:
        assert image.size == (720, 1280)


def test_real_grid_generation_uses_article_appendix_e(tmp_path: Path, monkeypatch) -> None:
    captured = []

    def fake_generate_grid(prompt, output_path, settings):
        captured.append(prompt)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1536, 864), (220, 210, 200)).save(output_path)
        return output_path

    monkeypatch.setattr(scene_images, "_generate_grid_api", fake_generate_grid)
    manifest_path, grids, scenes = generate_scene_images(
        "第一句。第二句。第三句。第四句。第五句。第六句。第七句。第八句。第九句。",
        count=9,
        task_dir=tmp_path,
        version=1,
        settings=_settings(tmp_path),
        demo=False,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(grids) == 1
    assert len(scenes) == 9
    assert IMAGE_SYSTEM_PROMPT in captured[0]
    assert "1. 第一句。" in captured[0]
    assert "2. 第二句。" in captured[0]
    assert "阅读、书桌、散步、运动、厨房" not in captured[0]
    assert manifest["prompt_version"] == IMAGE_PROMPT_VERSION
    assert manifest["prompt_snapshots"][0]["full_prompt"] == captured[0]

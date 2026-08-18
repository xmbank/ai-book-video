import json
from pathlib import Path

from book_video_workbench.storyboard import build_storyboard


def test_storyboard_scene_boundaries_do_not_overlap(tmp_path: Path) -> None:
    subtitle_path = tmp_path / "subtitles.json"
    tts_path = tmp_path / "tts.wav"
    subtitle_path.write_text("{}", encoding="utf-8")
    tts_path.write_bytes(b"wav")
    storyboard_path, _ = build_storyboard(
        tmp_path,
        title="source",
        book_title="book",
        hook="hook",
        duration=10.538,
        subtitle_path=subtitle_path,
        tts_path=tts_path,
        source_video=None,
        book_cover=None,
    )
    scenes = json.loads(storyboard_path.read_text(encoding="utf-8"))["scenes"]
    for current, following in zip(scenes, scenes[1:]):
        assert round(current["start"] + current["duration"], 3) == following["start"]
    assert all(scene["foreground"].startswith("#") for scene in scenes)


def test_storyboard_uses_confirmed_selling_points_for_product_scenes(
    tmp_path: Path,
) -> None:
    subtitle_path = tmp_path / "subtitles.json"
    tts_path = tmp_path / "tts.wav"
    cover_path = tmp_path / "cover.jpg"
    subtitle_path.write_text("{}", encoding="utf-8")
    tts_path.write_bytes(b"wav")
    cover_path.write_bytes(b"cover")
    _, manifest_path = build_storyboard(
        tmp_path,
        title="source",
        book_title="抗老生活",
        hook="你以为抗老只靠护肤？",
        duration=12,
        subtitle_path=subtitle_path,
        tts_path=tts_path,
        source_video=None,
        book_cover=cover_path,
        scene_briefs=[
            {"shot_role": "pattern_interrupt"},
            {"shot_role": "product_reveal"},
            {"shot_role": "human_action"},
            {"shot_role": "closing"},
        ],
        scene_images=[tmp_path / f"scene-{index}.jpg" for index in range(4)],
        style_id="book-sales",
        selling_points=["关注日常习惯", "提供生活方式观察"],
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["content"]["selling_points"] == [
        "关注日常习惯",
        "提供生活方式观察",
    ]
    assert manifest["scenes"][1]["headline"] == "关注日常习惯"
    assert manifest["scenes"][-1]["product_scene"] is True

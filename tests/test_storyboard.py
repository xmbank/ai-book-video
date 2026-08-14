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

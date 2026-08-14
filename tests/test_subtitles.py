from book_video_workbench.subtitles import (
    proportional_timeline,
    split_caption_text,
    timeline_from_transcript,
    validate_timeline,
)


def test_split_caption_text_preserves_all_text() -> None:
    source = "越着急的人，越容易浪费时间。这本书讲长期积累。"
    result = split_caption_text(source, max_chars=10)
    assert "".join(result) == source
    assert all(len(item) <= 10 for item in result)


def test_proportional_timeline_covers_audio_without_overlap() -> None:
    timeline = proportional_timeline("第一句话。第二句话更长一些。最后一句。", 8.25)
    validate_timeline(timeline, 8.25)
    assert timeline[0]["start"] == 0
    assert timeline[-1]["end"] == 8.25


def test_split_transcript_segment_gets_non_overlapping_time_slices() -> None:
    transcript = {
        "segments": [
            {
                "start": 0.0,
                "end": 5.0,
                "text": "这是一条明显超过十四个汉字而且需要拆分的字幕文本。",
            }
        ]
    }
    timeline = timeline_from_transcript(transcript, 5.0)
    validate_timeline(timeline, 5.0)
    assert len(timeline) > 1
    assert timeline[-1]["end"] == 5.0

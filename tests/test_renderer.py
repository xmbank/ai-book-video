from book_video_workbench.renderer import _headline_html


def test_long_headline_breaks_at_punctuation() -> None:
    assert _headline_html("把一件小事，长期做对") == "把一件小事，<br>长期做对"


def test_short_headline_stays_on_one_line() -> None:
    assert _headline_html("一本好书") == "一本好书"

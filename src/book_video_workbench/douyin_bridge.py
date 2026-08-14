from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable

import requests


REQUEST_ATTEMPTS = 4
RETRY_DELAYS = (1.0, 2.0, 4.0)


def fetch_share_page(
    session: requests.Session,
    url: str,
    *,
    user_agent: str,
    request_timeout: float = 30,
    sleep: Callable[[float], None] = time.sleep,
    curl_path: str | None = None,
) -> tuple[str, str]:
    """Fetch a Douyin share page with requests retries and a curl fallback."""
    errors: list[str] = []
    for attempt in range(REQUEST_ATTEMPTS):
        try:
            response = session.get(url, allow_redirects=True, timeout=request_timeout)
            response.raise_for_status()
            return str(response.url), response.text
        except requests.RequestException as exc:
            errors.append(f"requests attempt {attempt + 1}: {exc}")
            if attempt < len(RETRY_DELAYS):
                sleep(RETRY_DELAYS[attempt])

    curl = curl_path or shutil.which("curl")
    if not curl:
        errors.append("curl fallback: curl command not found")
        raise RuntimeError("DOUYIN_SHARE_FETCH_FAILED\n" + "\n".join(errors))

    with tempfile.TemporaryDirectory(prefix="douyin-share-") as directory:
        output = Path(directory) / "share.html"
        try:
            proc = subprocess.run(
                [
                    curl,
                    "--silent",
                    "--show-error",
                    "--fail",
                    "--location",
                    "--compressed",
                    "--retry",
                    "3",
                    "--retry-all-errors",
                    "--retry-delay",
                    "1",
                    "--connect-timeout",
                    "20",
                    "--max-time",
                    "90",
                    "--user-agent",
                    user_agent,
                    "--header",
                    "Accept-Language: zh-CN,zh;q=0.9",
                    "--output",
                    str(output),
                    "--write-out",
                    "%{url_effective}",
                    url,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=100,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"curl fallback: {exc}")
        else:
            final_url = (proc.stdout or "").strip()
            if proc.returncode == 0 and final_url and output.is_file():
                html = output.read_text(encoding="utf-8", errors="replace")
                if html:
                    return final_url, html
            detail = (proc.stderr or proc.stdout or "no output").strip()
            errors.append(f"curl fallback ({proc.returncode}): {detail[-3000:]}")

    raise RuntimeError("DOUYIN_SHARE_FETCH_FAILED\n" + "\n".join(errors))


def resolve_metadata(backend_dir: Path, share_text: str) -> dict:
    sys.path.insert(0, str(backend_dir))
    import script.douyin_resolver as d

    session = d._session()
    share_url = d.expand_share_url(share_text)
    fetch_url = d.normalize_to_share_page(share_url)
    _, html = fetch_share_page(
        session,
        fetch_url,
        user_agent=d.SHARE_PAGE_UA,
    )

    item = None
    for parser in (d._parse_router_data, d._parse_render_data):
        payload = parser(html)
        if payload:
            items = d._find_item_list(payload)
            if items:
                item = items[0]
                break
    if item is None:
        raise RuntimeError("分享页未找到作品数据")

    meta = asdict(d._meta_from_aweme_item(item, share_url))
    stats = item.get("statistics") or {}
    video = item.get("video") or {}
    author = item.get("author") or {}
    meta.update(
        {
            "description": item.get("desc") or meta.get("title") or "",
            "published_at_unix": item.get("create_time"),
            "duration_ms": video.get("duration"),
            "author_id": author.get("uid")
            or author.get("sec_uid")
            or author.get("unique_id"),
            "metrics": {
                "play": stats.get("play_count"),
                "like": stats.get("digg_count"),
                "comment": stats.get("comment_count"),
                "collect": stats.get("collect_count"),
                "share": stats.get("share_count"),
            },
        }
    )
    return meta


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: douyin_bridge BACKEND_DIR SHARE_TEXT")
    meta = resolve_metadata(Path(sys.argv[1]), sys.argv[2])
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()

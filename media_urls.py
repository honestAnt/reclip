"""Rewrite site URLs into the canonical forms yt-dlp extractors accept."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_DOUYIN_HOSTS = {
    "douyin.com",
    "www.douyin.com",
    "m.douyin.com",
    "iesdouyin.com",
    "www.iesdouyin.com",
}

# modal_id is the video currently open in the overlay; vid is often an older clip on the same page.
_DOUYIN_ID_KEYS = ("modal_id", "vid", "aweme_id", "item_ids")
_DOUYIN_PATH_ID = re.compile(r"/(?:video|note|share/video)/(\d+)")
_DOUYIN_QUERY_ID = re.compile(
    r"(?:[?&]|%26|&amp;)(?P<key>modal_id|vid|aweme_id|item_ids)=(?P<id>\d+)",
    re.I,
)
_XHS_PROFILE_NOTE = re.compile(r"/user/profile/[^/]+/([0-9a-f]+)", re.I)


def _bare_host(host: str) -> str:
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def _clean_url(url: str) -> str:
    return (url or "").strip().replace("&amp;", "&")


def _merged_query(parsed) -> dict[str, list[str]]:
    qs = parse_qs(parsed.query, keep_blank_values=False)
    if parsed.fragment and "=" in parsed.fragment:
        qs.update(parse_qs(parsed.fragment, keep_blank_values=False))
    return qs


def _first_digit_id(values: list[str] | None) -> str | None:
    for value in values or []:
        if value and value.isdigit():
            return value
    return None


def douyin_video_id(url: str) -> str | None:
    url = _clean_url(url)
    parsed = urlparse(url)
    qs = _merged_query(parsed)
    for key in _DOUYIN_ID_KEYS:
        found = _first_digit_id(qs.get(key))
        if found:
            return found
    match = _DOUYIN_PATH_ID.search(parsed.path or "")
    if match:
        return match.group(1)
    found_by_key = {}
    for match in _DOUYIN_QUERY_ID.finditer(url):
        found_by_key.setdefault(match.group("key").lower(), match.group("id"))
    for key in _DOUYIN_ID_KEYS:
        if key in found_by_key:
            return found_by_key[key]
    return None


def normalize_media_url(url: str) -> str:
    """Turn profile/share/modal links into the video URL yt-dlp expects."""
    url = _clean_url(url)
    if not url:
        return url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in _DOUYIN_HOSTS:
        video_id = douyin_video_id(url)
        if video_id:
            return f"https://www.douyin.com/video/{video_id}"
        return url
    if _bare_host(host) == "xiaohongshu.com":
        match = _XHS_PROFILE_NOTE.search(parsed.path or "")
        if match:
            return f"https://www.xiaohongshu.com/explore/{match.group(1)}"
    return url


def unsupported_profile_message(url: str) -> str | None:
    parsed = urlparse(_clean_url(url))
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if host in _DOUYIN_HOSTS and "/user/" in path and not douyin_video_id(url):
        return (
            "This is a Douyin profile page, not a video. "
            "Open the video and copy a link that contains /video/, modal_id=, or vid=."
        )
    if _bare_host(host) == "xiaohongshu.com" and "/user/profile/" in path:
        if not _XHS_PROFILE_NOTE.search(path):
            return (
                "This is a Xiaohongshu profile page, not a note. "
                "Open the note/video and copy that link."
            )
    return None

"""Regression tests for OpenFind's ordered video-extraction algorithm.

The pipeline is intentionally network-mocked: direct media URLs are handled first,
TeraBox shares use the dedicated resolver, site pages use yt-dlp, and supported
social hosts may fall back to gallery-dl. These tests do not bypass login or DRM.
"""
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
import main

client = TestClient(main.app)


def test_direct_video_url_uses_fast_path_without_running_extractor():
    url = "https://cdn.example.org/media/clip.mp4?token=abc"
    with patch("main._assert_public_video_url", return_value=url), \
         patch.dict(sys.modules, {"yt_dlp": None}), \
         patch("main._gallery_dl_probe") as gallery:
        response = client.get("/api/video-resolve", params={"url": url})

    assert response.status_code == 200
    data = response.json()
    assert data["extractor"] == "Direct media URL"
    assert data["ext"] == "mp4"
    assert data["preview_url"] == url
    assert data["download_url"].startswith("/api/video-download?url=")
    gallery.assert_not_called()


def test_direct_download_streams_as_attachment_and_preserves_range_headers():
    url = "https://cdn.example.org/media/clip.webm"

    class FakeResponse:
        status = 206
        headers = {
            "Content-Type": "video/webm",
            "Content-Length": "4",
            "Content-Range": "bytes 0-3/100",
            "Accept-Ranges": "bytes",
        }
        closed = False

        def __init__(self):
            self.chunks = [b"data", b""]

        def read(self, size=-1):
            return self.chunks.pop(0)

        def close(self):
            self.closed = True

    upstream = FakeResponse()
    with patch("main._assert_public_video_url", return_value=url), \
         patch("main._open_direct_video_response", return_value=(upstream, url)) as open_media:
        response = client.get(
            "/api/video-download",
            params={"url": url},
            headers={"Range": "bytes=0-3"},
        )

    assert response.status_code == 206
    assert response.content == b"data"
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["content-range"] == "bytes 0-3/100"
    assert response.headers["accept-ranges"] == "bytes"
    assert upstream.closed
    open_media.assert_called_once_with(url, "bytes=0-3")


def test_page_extraction_uses_ytdlp_metadata_and_does_not_expose_expiring_stream():
    url = "https://video.example.org/watch/123"
    fake_info = {
        "title": "Example clip",
        "id": "123",
        "extractor_key": "Example",
        "ext": "mp4",
        "filesize_approx": 2048,
        "duration": 31,
        "thumbnail": "https://img.example.org/poster.jpg",
        "url": "https://temporary-cdn.example.org/signed-stream?secret=short-lived",
    }

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, target, download=False):
            assert target == url
            assert download is False
            return fake_info

    fake_module = SimpleNamespace(YoutubeDL=FakeYoutubeDL)
    with patch("main._assert_public_video_url", return_value=url), \
         patch.dict(sys.modules, {"yt_dlp": fake_module}):
        response = client.get("/api/video-resolve", params={"url": url})

    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "Example clip"
    assert data["size_bytes"] == 2048
    assert data["duration"] == 31
    assert data["thumbnail"] == "https://img.example.org/poster.jpg"
    assert data["preview_url"] is None
    assert "temporary-cdn.example.org" not in response.text
    assert data["download_url"].startswith("/api/video-download?url=")


def test_social_extractor_fallback_is_used_only_after_ytdlp_failure():
    url = "https://www.instagram.com/p/example/"
    fake_module = SimpleNamespace(
        YoutubeDL=lambda options: (_ for _ in ()).throw(RuntimeError("unsupported URL"))
    )
    fallback = {
        "title": "Social clip",
        "webpage_url": url,
        "extractor": "gallery-dl",
        "ext": "unknown",
        "size_bytes": None,
        "size": "Unknown",
        "duration": None,
        "thumbnail": None,
        "download_url": "/api/video-download?url=encoded",
        "preview_url": None,
        "note": "Fallback",
    }
    with patch("main._assert_public_video_url", return_value=url), \
         patch.dict(sys.modules, {"yt_dlp": fake_module}), \
         patch("main._gallery_dl_probe", return_value=fallback) as gallery:
        response = client.get("/api/video-resolve", params={"url": url})

    assert response.status_code == 200
    assert response.json()["extractor"] == "gallery-dl"
    gallery.assert_called_once_with(url)


def test_video_scanner_reports_public_media_candidates_without_fetching_private_hosts():
    source = """
    <html><head><title>Demo</title></head>
    <body><video><source src="/clips/demo.mp4"></video></body></html>
    """
    parser, scripts, styles, pages = main._parse_video_html(
        source, "https://media.example.org/watch"
    )
    assert parser.title_parts == ["Demo"]
    assert "/clips/demo.mp4" in parser.media_urls
    assert scripts == []
    assert styles == []
    assert pages == []


def test_completed_file_filter_excludes_subtitles_thumbnails_and_audio_only(tmp_path):
    video = tmp_path / "clip.mp4"
    subtitle = tmp_path / "captions.srt"
    audio = tmp_path / "audio.m4a"
    thumbnail = tmp_path / "cover.jpg"
    for path in (video, subtitle, audio, thumbnail):
        path.write_bytes(b"x" * 12000)

    completed = main._completed_video_files(tmp_path)

    assert completed == [video]


def test_direct_download_revalidates_redirect_targets():
    import urllib.error
    from email.message import Message

    url = "https://cdn.example.org/clip.mp4"
    headers = Message()
    headers["Location"] = "http://127.0.0.1/private.mp4"
    redirect = urllib.error.HTTPError(url, 302, "Found", headers, None)

    class FakeOpener:
        calls = 0

        def open(self, request, timeout=0):
            self.calls += 1
            raise redirect

    opener = FakeOpener()
    with patch("main.urllib.request.build_opener", return_value=opener), \
         patch("main._assert_public_video_url", side_effect=lambda value: (
             value if value == url else (_ for _ in ()).throw(ValueError("Private, local, or reserved network addresses are blocked."))
         )):
        try:
            main._open_direct_video_response(url)
        except ValueError as exc:
            assert "Private" in str(exc)
        else:
            raise AssertionError("redirect to a private address must be rejected")

    assert opener.calls == 1


def test_media_signature_rejects_html_disguised_as_mp4():
    assert main._media_magic(b"<html><body>blocked</body></html>", "video/mp4") == (
        "HTML/text response (not a video file)"
    )
    assert main._media_magic(b"\x00\x00\x00\x18ftypisom") == "MP4/MOV container"

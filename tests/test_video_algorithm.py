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


def test_direct_download_redirects_to_validated_media_url():
    url = "https://cdn.example.org/media/clip.webm"
    with patch("main._assert_public_video_url", return_value=url):
        response = client.get(
            "/api/video-download",
            params={"url": url},
            follow_redirects=False,
        )
    assert response.status_code == 302
    assert response.headers["location"] == url


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


def test_media_signature_rejects_html_disguised_as_mp4():
    assert main._media_magic(b"<html><body>blocked</body></html>", "video/mp4") == (
        "HTML/text response (not a video file)"
    )
    assert main._media_magic(b"\x00\x00\x00\x18ftypisom") == "MP4/MOV container"

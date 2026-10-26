from unittest.mock import patch
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_index_is_served():
    response = client.get("/")
    assert response.status_code == 200
    assert "OpenFind" in response.text


def test_suggestions_are_local_and_bounded():
    response = client.get("/api/suggest", params={"q": "python"})
    assert response.status_code == 200
    suggestions = response.json()["suggestions"]
    assert "python" in suggestions
    assert len(suggestions) <= 5


def test_search_normalizes_and_paginates():
    results = [
        {"title": "One", "href": "https://example.com/one", "body": "First"},
        {"title": "Two", "href": "https://example.com/two", "body": "Second"},
    ]
    with patch("main._search_sync", return_value=results) as mocked:
        response = client.get("/api/search", params={"q": " hello  world ", "page": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "hello world"
    assert body["page"] == 2
    assert body["results"][0]["url"] == "https://example.com/one"
    mocked.assert_called_once()


def test_rejects_invalid_search_category():
    response = client.get("/api/search", params={"q": "test", "category": "unknown"})
    assert response.status_code == 422


def test_math_answer_is_safe():
    assert main._instant_answer("what is 2 + 3 * 4")["value"] == "14"
    assert main._instant_answer("__import__('os').system('whoami')") is None


def test_url_normalization_rejects_javascript():
    assert main._normalize_result({"title": "bad", "href": "javascript:alert(1)"}, "web") is None


def test_provider_error_is_sanitized():
    with patch("main._search_sync", side_effect=RuntimeError("private details")):
        response = client.get("/api/search", params={"q": "test"})
    assert response.status_code == 503
    assert "private details" not in response.text



def test_spelling_suggestion_is_conservative():
    assert main._spell_correction("restarant") == "restaurant"
    assert main._spell_correction("OpenFind OSINT") is None


def test_public_target_validation():
    assert main._public_target("openai.com") == ("openai.com", "domain")
    assert main._public_target("8.8.8.8") == ("8.8.8.8", "ip")
    try:
        main._public_target("127.0.0.1")
    except ValueError:
        pass
    else:
        raise AssertionError("private IP must be rejected")


def test_osint_rejects_local_targets():
    response = client.get("/api/osint", params={"target": "127.0.0.1"})
    assert response.status_code == 422


def test_searxng_result_normalization():
    result = main._normalize_result(
        {"title": "Example", "url": "https://example.org/", "content": "Public result", "engine": "test"},
        "web",
    )
    assert result is not None
    assert result["source"] == "test"
    assert result["snippet"] == "Public result"



def test_video_finder_route():
    response = client.get("/video")
    assert response.status_code == 200
    assert "Video downloader" in response.text
    assert "Download video" in response.text


def test_video_parser_discovers_media_scripts_styles_and_subpages():
    source = """
    <html><head><title>Public clips</title>
    <link rel="stylesheet" href="/assets/player.css">
    <script src="/assets/player.js"></script></head>
    <body><video controls><source src="/media/trailer.mp4" type="video/mp4"></video>
    <a href="/watch/episode-2">Next episode</a></body></html>
    """
    parser, scripts, styles, pages = main._parse_video_html(source, "https://media.example.org/")
    assert parser.title_parts == ["Public clips"]
    assert "/media/trailer.mp4" in parser.media_urls
    assert scripts == ["https://media.example.org/assets/player.js"]
    assert styles == ["https://media.example.org/assets/player.css"]
    assert pages == ["https://media.example.org/watch/episode-2"]


def test_video_kind_recognizes_downloads_and_stream_manifests():
    assert main._video_kind("https://cdn.example.org/clip.MP4") == "MP4 video"
    assert main._video_kind("https://cdn.example.org/live.m3u8") == "HLS stream playlist"
    assert main._video_kind("https://cdn.example.org/watch?id=2") is None


def test_video_scanner_blocks_private_addresses():
    for value in ("http://127.0.0.1/", "http://10.0.0.8/", "http://localhost/"):
        try:
            main._assert_public_video_url(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"private URL should be rejected: {value}")

"""OpenFind: lightweight FastAPI metasearch."""
from __future__ import annotations

import ast
import ipaddress
import json
import urllib.parse
import urllib.request
import asyncio
import logging
import math
import operator
import os
import re
import socket
import shutil
import tempfile
import subprocess
import urllib.error
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from ddgs import DDGS
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask
from spellchecker import SpellChecker

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "templates" / "index.html"
PAGE_SIZE = 20
MAX_PAGE = 25
UPSTREAM_TIMEOUT = 8
MAX_CONCURRENT_SEARCHES = 4

Category = Literal["web", "news", "images", "videos"]
SafeSearch = Literal["on", "moderate", "off"]

logging.basicConfig(level=os.getenv("LOG_LEVEL", "WARNING").upper())
logger = logging.getLogger("openfind")
app = FastAPI(
    title="OpenFind",
    description="A lightweight, privacy-conscious metasearch interface powered by DDGS.",
    version="1.0.0",
    docs_url="/docs" if os.getenv("ENABLE_API_DOCS", "false").lower() == "true" else None,
    redoc_url=None,
)
search_slots = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)

spell = SpellChecker(distance=1)
TECH_WORDS = {
    "ddgs", "searxng", "openfind", "osint", "onion", "onions", "tor",
    "fastapi", "uvicorn", "pydantic", "arduino", "raspberry", "raspberrypi",
    "nodemcu", "nodejs", "javascript", "typescript", "python", "django",
    "flask", "github", "gitlab", "render", "docker", "kubernetes", "linux",
    "android", "ios", "cpp", "api", "apis", "dns", "rdap", "http", "https",
    "urlscan", "virustotal", "searx", "cybersecurity", "metasearch",
}
spell.word_frequency.load_words(TECH_WORDS)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    if not INDEX_FILE.is_file():
        raise HTTPException(status_code=500, detail="Frontend template is missing.")
    return FileResponse(INDEX_FILE, media_type="text/html; charset=utf-8")


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "openfind"}


def _safe_http_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value.strip()


def _clean_text(value: Any, limit: int = 700) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    return str(value).strip()[:limit]


def _normalize_result(item: dict[str, Any], category: str) -> dict[str, str] | None:
    title = _clean_text(item.get("title") or item.get("name"), 300)
    href = _safe_http_url(item.get("href") or item.get("url") or item.get("link"))
    if not title or not href:
        return None
    nested = item.get("images") if isinstance(item.get("images"), dict) else {}
    image_url = _safe_http_url(
        item.get("thumbnail") or item.get("image") or item.get("thumbnail_url")
        or nested.get("medium") or nested.get("small")
    ) if category in {"images", "videos"} else None
    source = _clean_text(item.get("source") or item.get("publisher") or item.get("provider") or item.get("engine"), 150)
    if not source:
        source = (urlparse(href).hostname or "").removeprefix("www.")
    result = {
        "title": title,
        "url": href,
        "snippet": _clean_text(item.get("body") or item.get("description") or item.get("content"), 700),
        "source": source,
        "date": _clean_text(item.get("date") or item.get("published"), 80),
    }
    if image_url:
        result["image"] = image_url
    return result


def _search_sync(query: str, category: str, page: int, safesearch: str) -> list[dict[str, Any]]:
    with DDGS(timeout=UPSTREAM_TIMEOUT) as client:
        method = {"web": client.text, "news": client.news, "images": client.images, "videos": client.videos}[category]
        return method(
            query=query, region="wt-wt", safesearch=safesearch,
            max_results=PAGE_SIZE + 1, page=page, backend="auto",
        )


_BINARY = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _math_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _math_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        value = float(node.value)
        if not math.isfinite(value) or abs(value) > 1e100:
            raise ValueError("number out of bounds")
        return value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_math_node(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left, right = _math_node(node.left), _math_node(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 8 or abs(left) > 1e6):
            raise ValueError("exponent out of bounds")
        value = _BINARY[type(node.op)](left, right)
        if not math.isfinite(value) or abs(value) > 1e100:
            raise ValueError("result out of bounds")
        return value
    raise ValueError("unsupported expression")


def _instant_answer(query: str) -> dict[str, str] | None:
    candidate = re.sub(r"^\s*(?:calculate|calc|what\s+is)\s+", "", query.strip(), flags=re.I).rstrip("? ")
    if not candidate or len(candidate) > 80 or not re.fullmatch(r"[\d\s().+*/%\-]+", candidate):
        return None
    if not re.search(r"[+*/%\-]", candidate):
        return None
    try:
        tree = ast.parse(candidate, mode="eval")
        if sum(1 for _ in ast.walk(tree)) > 40:
            return None
        value = _math_node(tree)
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError, TypeError):
        return None
    display = str(int(value)) if value.is_integer() and abs(value) < 1e16 else f"{value:.10g}"
    return {"title": "Calculator", "value": display, "kind": "calculation"}


def _search_via_searxng(query: str, category: str, page: int, safesearch: str) -> list[dict[str, Any]]:
    """Use an operator-configured SearXNG instance; never depend on a random public instance."""
    base = os.getenv("SEARXNG_URL", "").strip().rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError("Invalid SearXNG URL configuration")
    categories = {"web": "general", "news": "news", "images": "images", "videos": "videos"}
    params = urllib.parse.urlencode({
        "q": query, "format": "json", "categories": categories[category],
        "pageno": str(page), "safesearch": {"on": "2", "moderate": "1", "off": "0"}[safesearch],
    })
    request = urllib.request.Request(
        base + "/search?" + params,
        headers={"User-Agent": "OpenFind/1.1 (+https://github.com/DilipDC/openfind)", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT) as response:
        payload = json.loads(response.read(2_000_000).decode("utf-8"))
    result_rows = payload.get("results", [])
    if not isinstance(result_rows, list):
        raise RuntimeError("Unexpected SearXNG response")
    return [row for row in result_rows if isinstance(row, dict)][:PAGE_SIZE + 1]


def _edit_distance(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for i, char_left in enumerate(left, start=1):
        current = [i]
        for j, char_right in enumerate(right, start=1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (char_left != char_right)))
        previous = current
    return previous[-1]


def _spell_correction(query: str) -> str | None:
    """Return a conservative suggestion; never silently rewrite the original query."""
    if not query or len(query) > 200:
        return None
    replacements: dict[str, str] = {}
    for match in re.finditer(r"\b[A-Za-z]{4,}\b", query):
        original = match.group(0)
        word = original.lower()
        if word in TECH_WORDS or spell.known([word]) or (original.isupper() and len(original) <= 6):
            continue
        candidate = spell.correction(word)
        if not candidate or candidate == word:
            continue
        if _edit_distance(word, candidate) > (1 if len(word) <= 5 else 2):
            continue
        if original[0].isupper():
            candidate = candidate.capitalize()
        replacements[original] = candidate
    corrected = query
    for original, candidate in replacements.items():
        corrected = re.sub(r"\b" + re.escape(original) + r"\b", candidate, corrected, count=1)
    return corrected if corrected.casefold() != query.casefold() else None


@app.get("/api/spell")
async def spell_suggestion(q: str = Query(..., min_length=1, max_length=200)) -> JSONResponse:
    query = " ".join(q.split())
    return JSONResponse({"query": query, "correction": _spell_correction(query)})


@app.get("/api/suggest")
async def suggest(q: str = Query(default="", max_length=120)) -> JSONResponse:
    query = " ".join(q.split())[:120]
    if len(query) < 2:
        return JSONResponse({"suggestions": []})
    terms = query.lower()
    options = [query]
    options += [f"{query} {suffix}" for suffix in (
        ["tutorial", "documentation", "examples"] if len(query.split()) <= 2
        else ["explained", "guide", "latest news"]
    )]
    if any(x in terms for x in ("python", "fastapi", "arduino", "raspberry pi", "cpp", "c++")):
        options.append(f"{query} official documentation")
    correction = _spell_correction(query)
    if correction:
        options.insert(0, correction)
    return JSONResponse({"suggestions": list(dict.fromkeys(x.strip() for x in options if x.strip()))[:6]})


@app.get("/api/search")
async def search(
    q: str = Query(..., min_length=1, max_length=200),
    category: Category = "web",
    page: int = Query(default=1, ge=1, le=MAX_PAGE),
    safesearch: SafeSearch = "moderate",
) -> JSONResponse:
    query = " ".join(q.split())
    if not query:
        raise HTTPException(status_code=422, detail="Enter a search query.")
    async with search_slots:
        search_source = "DDGS metasearch"
        raw: list[dict[str, Any]] | None = None
        if os.getenv("SEARXNG_URL", "").strip():
            try:
                raw = await asyncio.to_thread(_search_via_searxng, query, category, page, safesearch)
                search_source = "SearXNG"
            except Exception as exc:
                logger.warning("Configured SearXNG failed; using DDGS (%s)", type(exc).__name__)
        if raw is None:
            try:
                raw = await asyncio.to_thread(_search_sync, query, category, page, safesearch)
            except Exception as exc:
                logger.warning("Metasearch provider failure: %s", type(exc).__name__)
                raise HTTPException(status_code=503, detail="Search is temporarily unavailable. Please try again shortly.") from None
    raw = raw or []
    has_next = len(raw) > PAGE_SIZE or (search_source == "SearXNG" and len(raw) >= PAGE_SIZE)
    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in raw[:PAGE_SIZE]:
        normalized = _normalize_result(item, category) if isinstance(item, dict) else None
        if not normalized:
            continue
        parsed = urlparse(normalized["url"])
        canonical = parsed._replace(query="", fragment="").geturl().rstrip("/")
        if canonical in seen_urls:
            continue
        seen_urls.add(canonical)
        results.append(normalized)
    return JSONResponse({
        "query": query, "category": category, "page": page, "page_size": PAGE_SIZE,
        "max_page": MAX_PAGE, "result_count": len(results), "has_next": has_next,
        "search_source": search_source, "results": results, "instant_answer": _instant_answer(query),
    })


def _http_json(url: str, timeout: float = 3.5) -> dict[str, Any]:
    request = urllib.request.Request(
        url, headers={
            "User-Agent": "OpenFind/1.1 (+https://github.com/DilipDC/openfind)",
            "Accept": "application/rdap+json, application/dns-json, application/json",
        }
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read(1_000_000)
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Unexpected JSON response")
    return data


def _dns_lookup(domain: str, record_type: str) -> list[dict[str, Any]]:
    url = "https://dns.google/resolve?" + urllib.parse.urlencode({"name": domain, "type": record_type})
    payload = _http_json(url)
    return [
        {"data": _clean_text(answer.get("data"), 500), "ttl": int(answer.get("TTL", 0) or 0)}
        for answer in payload.get("Answer", [])
        if isinstance(answer, dict) and answer.get("data")
    ][:12]


def _public_target(target: str) -> tuple[str, str]:
    candidate = target.strip().rstrip(".")
    if not candidate or len(candidate) > 253 or any(ch in candidate for ch in ("/", "\\", "@", " ", "\t", "\n")):
        raise ValueError("Enter a public domain name or public IP address.")
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        address = None
    if address is not None:
        if not address.is_global:
            raise ValueError("Private, reserved, or local IP addresses are not supported.")
        return str(address), "ip"
    try:
        domain = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("Enter a valid public domain name or public IP address.") from None
    if domain.endswith(".onion") or domain in {"localhost", "example.com", "example.net", "example.org"}:
        raise ValueError("Use a public clearnet domain or public IP. Onion links are searched through the separate Onion Index tab.")
    labels = domain.split(".")
    if len(labels) < 2 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        raise ValueError("Enter a valid public domain name or public IP address.")
    if labels[-1] in {"local", "internal", "test", "invalid", "localhost"} or all(label.isdigit() for label in labels):
        raise ValueError("Local and reserved domains are not supported.")
    return domain, "domain"


def _rdap_summary(target: str, target_type: str) -> dict[str, Any]:
    safe_target = urllib.parse.quote(target, safe=".-:")
    path = "ip" if target_type == "ip" else "domain"
    data = _http_json("https://rdap.org/" + path + "/" + safe_target)
    events = [
        {"action": _clean_text(event.get("eventAction"), 80), "date": _clean_text(event.get("eventDate"), 80)}
        for event in data.get("events", [])[:12] if isinstance(event, dict)
    ]
    nameservers = [
        _clean_text(server["ldhName"], 255).lower()
        for server in data.get("nameservers", [])[:16]
        if isinstance(server, dict) and server.get("ldhName")
    ]
    registrar = ""
    for entity in data.get("entities", [])[:20]:
        if not isinstance(entity, dict) or "registrar" not in entity.get("roles", []):
            continue
        vcard = entity.get("vcardArray")
        if isinstance(vcard, list) and len(vcard) > 1 and isinstance(vcard[1], list):
            for entry in vcard[1]:
                if isinstance(entry, list) and len(entry) > 3 and entry[0] == "fn":
                    registrar = _clean_text(entry[3], 200)
                    break
        if registrar:
            break
    return {
        "name": _clean_text(data.get("name") or data.get("ldhName"), 255),
        "handle": _clean_text(data.get("handle"), 120), "registrar": registrar,
        "status": [_clean_text(value, 100) for value in data.get("status", [])[:12] if isinstance(value, str)],
        "nameservers": nameservers, "events": events,
    }


async def _safe_lookup(function, *args):
    try:
        return await asyncio.to_thread(function, *args)
    except Exception as exc:
        logger.info("Passive OSINT source unavailable: %s", type(exc).__name__)
        return None


@app.get("/api/osint")
async def osint(target: str = Query(..., min_length=1, max_length=253)) -> JSONResponse:
    try:
        normalized, target_type = _public_target(target)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    async with search_slots:
        if target_type == "ip":
            rdap = await _safe_lookup(_rdap_summary, normalized, target_type)
            dns: dict[str, list[dict[str, Any]]] = {}
        else:
            tasks = [_safe_lookup(_dns_lookup, normalized, record_type) for record_type in ("A", "AAAA", "MX", "NS")]
            tasks.append(_safe_lookup(_rdap_summary, normalized, target_type))
            try:
                values = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.5)
            except TimeoutError:
                values = [None] * 5
            dns = {record_type: (values[index] or []) for index, record_type in enumerate(("A", "AAAA", "MX", "NS"))}
            rdap = values[4] if len(values) > 4 else None
    suffix = "domain" if target_type == "domain" else "ip"
    return JSONResponse({
        "target": normalized, "target_type": target_type, "dns": dns,
        "rdap": rdap or {}, "partial": not bool(rdap) and not any(dns.values()),
        "research_links": {
            "VirusTotal": "https://www.virustotal.com/gui/" + suffix + "/" + urllib.parse.quote(normalized, safe=".:"),
            "urlscan.io": "https://urlscan.io/search/#" + urllib.parse.quote(suffix + ":" + normalized, safe=""),
            **({"Certificate Transparency": "https://crt.sh/?q=" + urllib.parse.quote("%." + normalized, safe="")} if target_type == "domain" else {}),
        },
        "notice": "Passive public-source lookups only. No port scanning, crawling, login attempts, or access to private systems is performed. Data may be incomplete or delayed.",
    })


# --- Video URL discovery: bounded public-page inspection, not a proxy/DRM bypass.
VIDEO_TEMPLATE = BASE_DIR / "templates" / "video.html"
VIDEO_EXT_RE = re.compile(r"\.(?:mp4|m4v|webm|mov|mkv|avi|flv|ogv|3gp|m3u8|mpd)$", re.I)
VIDEO_URL_RE = re.compile(
    r"""(?P<url>(?:https?:)?//[^\s"'<>\\)]+?\.(?:mp4|m4v|webm|mov|mkv|avi|flv|ogv|3gp|m3u8|mpd)(?:[?#][^\s"'<>\\)]*)?|(?:\.\.?/|/)?[A-Za-z0-9_~!$&()+,;=:@%./-]{1,900}\.(?:mp4|m4v|webm|mov|mkv|avi|flv|ogv|3gp|m3u8|mpd)(?:[?#][^\s"'<>\\)]*)?)""",
    re.IGNORECASE,
)
VIDEO_CONTENT_TYPES = {
    "video/mp4": "MP4 video", "video/webm": "WebM video",
    "video/quicktime": "QuickTime video", "video/x-matroska": "Matroska video",
    "video/x-msvideo": "AVI video", "video/x-flv": "Flash video",
    "video/ogg": "Ogg video", "video/3gpp": "3GPP video",
    "application/vnd.apple.mpegurl": "HLS stream playlist",
    "application/x-mpegurl": "HLS stream playlist",
    "application/dash+xml": "MPEG-DASH stream manifest",
}
MAX_VIDEO_HTML_BYTES = 1_500_000
MAX_VIDEO_ASSET_BYTES = 900_000
MAX_VIDEO_PAGES = 5
MAX_VIDEO_SCRIPTS = 8
MAX_VIDEO_STYLES = 5
MAX_VIDEO_ITEMS = 40
MAX_VIDEO_HEAD_PROBES = 12


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


class _VideoPageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.media_urls = []
        self.script_urls = []
        self.style_urls = []
        self.page_urls = []
        self.inline_scripts = []
        self.inline_styles = []
        self.title_parts = []
        self.capture = None
        self.buffer = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        values = {key.lower(): (value or "") for key, value in attrs if key}
        if tag in {"video", "source", "audio", "track", "embed"}:
            for key in ("src", "data-src", "data-video", "data-url", "data-file", "file", "href"):
                if values.get(key):
                    self.media_urls.append(values[key])
        else:
            for key in ("data-video", "data-file", "data-media"):
                if values.get(key):
                    self.media_urls.append(values[key])
        if tag == "script":
            if values.get("src"):
                self.script_urls.append(values["src"])
            else:
                self.capture, self.buffer = "script", []
        elif tag == "style":
            self.capture, self.buffer = "style", []
        elif tag == "title":
            self.capture, self.buffer = "title", []
        if tag == "link" and "stylesheet" in values.get("rel", "").lower().split() and values.get("href"):
            self.style_urls.append(values["href"])
        if tag == "a" and values.get("href"):
            self.page_urls.append(values["href"])

    def handle_data(self, data):
        if self.capture:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == self.capture:
            value = "".join(self.buffer)
            if value.strip():
                if self.capture == "script":
                    self.inline_scripts.append(value)
                elif self.capture == "style":
                    self.inline_styles.append(value)
                elif self.capture == "title":
                    self.title_parts.append(value.strip())
            self.capture, self.buffer = None, []


def _assert_public_video_url(value):
    """Reject private/local destinations before requests and re-check redirects."""
    try:
        parsed = urllib.parse.urlsplit(value)
        scheme, hostname, port = parsed.scheme.lower(), parsed.hostname, parsed.port
    except ValueError:
        raise ValueError("Enter a valid public HTTP or HTTPS URL.") from None
    if scheme not in {"http", "https"} or not hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTP and HTTPS URLs are supported.")
    if hostname.lower().endswith(".onion") or "%" in hostname:
        raise ValueError("Onion, scoped, and non-public hostnames are not fetched.")
    allowed_port = 443 if scheme == "https" else 80
    if port is not None and port != allowed_port:
        raise ValueError("Only standard HTTP and HTTPS ports are supported.")
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            answers = socket.getaddrinfo(hostname, port or allowed_port, type=socket.SOCK_STREAM)
            addresses = [ipaddress.ip_address(answer[4][0].split("%", 1)[0]) for answer in answers]
        except (OSError, ValueError):
            raise ValueError("The host could not be resolved to a public address.") from None
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Private, local, or reserved network addresses are blocked.")
    return urllib.parse.urlunsplit((scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def _safe_video_fetch(value, max_bytes=MAX_VIDEO_HTML_BYTES, method="GET"):
    current = value
    opener = urllib.request.build_opener(_NoRedirect())
    for _ in range(4):
        _assert_public_video_url(current)
        request = urllib.request.Request(
            current,
            headers={
                "User-Agent": "OpenFind-VideoScout/1.0 (+https://github.com/DilipDC/openfind)",
                "Accept": "text/html,text/css,application/javascript,application/json,video/*,application/vnd.apple.mpegurl,application/dash+xml,*/*;q=0.4",
            },
            method=method,
        )
        try:
            with opener.open(request, timeout=6.0) as response:
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                body = response.read(max_bytes + 1) if method != "HEAD" else b""
                return response.geturl(), content_type, body[:max_bytes], response.status
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location", "")
            if exc.code in {301, 302, 303, 307, 308} and location:
                current = urllib.parse.urljoin(current, location)
                if exc.code == 303:
                    method = "GET"
                continue
            return current, exc.headers.get("Content-Type", "").split(";", 1)[0].strip().lower(), b"", exc.code
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise RuntimeError("The page or asset could not be fetched.") from exc
    raise RuntimeError("Too many redirects.")


def _video_kind(value, content_type=""):
    path = urllib.parse.urlsplit(value).path.lower()
    if VIDEO_EXT_RE.search(path):
        extension = path.rsplit(".", 1)[-1]
        return {
            "mp4": "MP4 video", "m4v": "M4V video", "webm": "WebM video",
            "mov": "QuickTime video", "mkv": "Matroska video", "avi": "AVI video",
            "flv": "Flash video", "ogv": "Ogg video", "3gp": "3GPP video",
            "m3u8": "HLS stream playlist", "mpd": "MPEG-DASH stream manifest",
        }.get(extension)
    return VIDEO_CONTENT_TYPES.get(content_type.lower())


def _resolve_video_url(raw, base_url):
    if not isinstance(raw, str) or len(raw) > 4096:
        return None
    value = unescape(raw).strip().strip("\"'").replace("\\/", "/")
    value = value.replace("\\u0026", "&").replace("\\x26", "&")
    if not value or value.startswith(("#", "data:", "blob:", "javascript:", "mailto:")):
        return None
    try:
        absolute = urllib.parse.urljoin(base_url, value)
        parsed = urllib.parse.urlsplit(absolute)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, ""))


def _same_video_origin(left, right):
    try:
        a, b = urllib.parse.urlsplit(left), urllib.parse.urlsplit(right)
        ap = a.port or (443 if a.scheme == "https" else 80)
        bp = b.port or (443 if b.scheme == "https" else 80)
        return a.scheme == b.scheme and (a.hostname or "").lower() == (b.hostname or "").lower() and ap == bp
    except ValueError:
        return False


def _video_candidates(source, base_url):
    normalized = unescape(source or "").replace("\\/", "/").replace("\\u0026", "&")
    found = []
    for match in VIDEO_URL_RE.finditer(normalized):
        value = _resolve_video_url(match.group("url"), base_url)
        if value:
            found.append(value)
        if len(found) >= MAX_VIDEO_ITEMS:
            break
    return list(dict.fromkeys(found))


def _parse_video_html(source, base_url):
    parser = _VideoPageParser()
    parser.feed(source)
    scripts = [_resolve_video_url(value, base_url) for value in parser.script_urls]
    styles = [_resolve_video_url(value, base_url) for value in parser.style_urls]
    pages = [_resolve_video_url(value, base_url) for value in parser.page_urls]
    scripts = list(dict.fromkeys(value for value in scripts if value))[:MAX_VIDEO_SCRIPTS]
    styles = list(dict.fromkeys(value for value in styles if value))[:MAX_VIDEO_STYLES]
    pages = list(dict.fromkeys(value for value in pages if value and _same_video_origin(value, base_url) and value != base_url))
    pages.sort(key=lambda value: 0 if re.search(r"(video|watch|media|movie|player|clip|stream|episode|embed)", urllib.parse.urlsplit(value).path, re.I) else 1)
    return parser, scripts, styles, pages[:MAX_VIDEO_PAGES - 1]


def _video_item(value, kind):
    parsed = urllib.parse.urlsplit(value)
    filename = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1]) or "video"
    return {
        "url": value, "title": filename[:160], "kind": kind,
        "host": parsed.hostname or "",
        "extension": "." + parsed.path.rsplit(".", 1)[-1].lower() if "." in parsed.path.rsplit("/", 1)[-1] else "",
        "stream": "true" if kind in {"HLS stream playlist", "MPEG-DASH stream manifest"} else "false",
    }


async def _video_scan_stream(target):
    found, checked = {}, set()
    head_probes = 0
    request_count = 0
    pages_scanned = scripts_scanned = styles_scanned = 0

    def line(payload):
        return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")

    def progress(percent, message):
        return line({"type": "progress", "percent": max(0, min(100, percent)), "message": message})

    async def add_media(raw, base, explicit=False):
        nonlocal head_probes, request_count
        value = _resolve_video_url(raw, base)
        if not value or value in checked or len(found) >= MAX_VIDEO_ITEMS:
            return None
        checked.add(value)
        kind = _video_kind(value)
        if not kind and explicit and head_probes < MAX_VIDEO_HEAD_PROBES:
            head_probes += 1
            request_count += 1
            try:
                final_url, content_type, _, status = await asyncio.to_thread(_safe_video_fetch, value, 0, "HEAD")
                kind, value = _video_kind(final_url, content_type), final_url
                if not kind and status in {405, 501}:
                    final_url, content_type, _, status = await asyncio.to_thread(_safe_video_fetch, value, 512, "GET")
                    kind, value = _video_kind(final_url, content_type), final_url
            except (RuntimeError, ValueError):
                return None
        if not kind:
            return None
        try:
            await asyncio.to_thread(_assert_public_video_url, value)
        except ValueError:
            return None
        item = _video_item(value, kind)
        if value not in found:
            found[value] = item
            return item
        return None

    yield progress(3, "Validating the public URL")
    try:
        direct_kind = _video_kind(target)
        if direct_kind:
            item = await add_media(target, target, explicit=True)
            if item:
                yield line({"type": "video", "video": item})
            yield progress(100, "Direct media URL checked")
            yield line({"type": "done", "videos": list(found.values()), "stats": {"pages": 0, "scripts": 0, "stylesheets": 0, "requests": request_count}, "message": f"Found {len(found)} video URL(s)."})
            return

        # Probe headers first so direct, extensionless media is not read into memory.
        request_count += 1
        head_url, head_type, _, head_status = await asyncio.to_thread(_safe_video_fetch, target, 0, "HEAD")
        root_kind = _video_kind(head_url, head_type)
        if root_kind and head_status < 400:
            item = await add_media(head_url, head_url, explicit=True)
            if item:
                yield line({"type": "video", "video": item})
            yield progress(100, "Direct media response checked")
            yield line({"type": "done", "videos": list(found.values()), "stats": {"pages": 0, "scripts": 0, "stylesheets": 0, "requests": request_count}, "message": f"Found {len(found)} video URL(s)."})
            return

        request_count += 1
        final_url, content_type, body, status = await asyncio.to_thread(_safe_video_fetch, target, MAX_VIDEO_HTML_BYTES, "GET")
        if status >= 400:
            yield line({"type": "error", "message": f"The page returned HTTP {status}. It may require login or deny automated requests."})
            return
        if _video_kind(final_url, content_type):
            item = await add_media(final_url, final_url, explicit=True)
            if item:
                yield line({"type": "video", "video": item})
            yield progress(100, "Direct media response checked")
            yield line({"type": "done", "videos": list(found.values()), "stats": {"pages": 1, "scripts": 0, "stylesheets": 0, "requests": request_count}, "message": f"Found {len(found)} video URL(s)."})
            return

        source = body.decode("utf-8", errors="replace")
        yield progress(10, "Parsing the first page: HTML and embedded player data")
        parser, script_urls, style_urls, page_urls = _parse_video_html(source, final_url)
        pages_scanned = 1
        page_title = " ".join(parser.title_parts).strip()[:180]
        for raw in parser.media_urls + _video_candidates(source, final_url):
            item = await add_media(raw, final_url, explicit=True)
            if item:
                yield line({"type": "video", "video": item})
        for blob in parser.inline_scripts + parser.inline_styles:
            for raw in _video_candidates(blob, final_url):
                item = await add_media(raw, final_url)
                if item:
                    yield line({"type": "video", "video": item})

        page_total = len(page_urls)
        for index, page_url in enumerate(page_urls, start=1):
            try:
                request_count += 1
                child_url, child_type, child_body, child_status = await asyncio.to_thread(_safe_video_fetch, page_url, MAX_VIDEO_HTML_BYTES, "GET")
                if child_status < 400 and "html" in child_type:
                    child_source = child_body.decode("utf-8", errors="replace")
                    child_parser, child_scripts, child_styles, _ = _parse_video_html(child_source, child_url)
                    pages_scanned += 1
                    script_urls = list(dict.fromkeys(script_urls + child_scripts))[:MAX_VIDEO_SCRIPTS]
                    style_urls = list(dict.fromkeys(style_urls + child_styles))[:MAX_VIDEO_STYLES]
                    for raw in child_parser.media_urls + _video_candidates(child_source, child_url):
                        item = await add_media(raw, child_url, explicit=True)
                        if item:
                            yield line({"type": "video", "video": item})
                    for blob in child_parser.inline_scripts + child_parser.inline_styles:
                        for raw in _video_candidates(blob, child_url):
                            item = await add_media(raw, child_url)
                            if item:
                                yield line({"type": "video", "video": item})
            except (RuntimeError, ValueError):
                pass
            yield progress(10 + int(index / max(page_total, 1) * 30), f"Scanned subpage {index}/{page_total}")

        script_total = len(script_urls)
        for index, script_url in enumerate(script_urls, start=1):
            try:
                request_count += 1
                _, _, script_body, script_status = await asyncio.to_thread(_safe_video_fetch, script_url, MAX_VIDEO_ASSET_BYTES, "GET")
                if script_status < 400:
                    scripts_scanned += 1
                    for raw in _video_candidates(script_body.decode("utf-8", errors="replace"), script_url):
                        item = await add_media(raw, script_url)
                        if item:
                            yield line({"type": "video", "video": item})
            except (RuntimeError, ValueError):
                pass
            yield progress(40 + int(index / max(script_total, 1) * 40), f"Scanned JavaScript {index}/{script_total}")

        style_total = len(style_urls)
        for index, style_url in enumerate(style_urls, start=1):
            try:
                request_count += 1
                _, _, style_body, style_status = await asyncio.to_thread(_safe_video_fetch, style_url, MAX_VIDEO_ASSET_BYTES, "GET")
                if style_status < 400:
                    styles_scanned += 1
                    for raw in _video_candidates(style_body.decode("utf-8", errors="replace"), style_url):
                        item = await add_media(raw, style_url)
                        if item:
                            yield line({"type": "video", "video": item})
            except (RuntimeError, ValueError):
                pass
            yield progress(80 + int(index / max(style_total, 1) * 15), f"Scanned CSS {index}/{style_total}")

        yield progress(100, "Scan finished")
        message = f"Found {len(found)} candidate video URL(s)." if found else "No directly exposed video URLs found. Some players load media only at runtime or require login."
        yield line({
            "type": "done", "videos": list(found.values()),
            "stats": {"pages": pages_scanned, "scripts": scripts_scanned, "stylesheets": styles_scanned, "requests": request_count},
            "page_title": page_title, "message": message,
        })
    except (RuntimeError, ValueError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "Could not fetch that public page. Check the URL and try again."
        yield line({"type": "error", "message": message})


@app.get("/video", include_in_schema=False)
async def video_finder() -> FileResponse:
    if not VIDEO_TEMPLATE.is_file():
        raise HTTPException(status_code=500, detail="Video finder page is missing.")
    return FileResponse(VIDEO_TEMPLATE, media_type="text/html; charset=utf-8")


@app.get("/api/video-scan", include_in_schema=False)
async def video_scan(url: str = Query(..., min_length=4, max_length=2048)) -> StreamingResponse:
    candidate = url.strip()
    if "://" not in candidate:
        candidate = "https://" + candidate
    try:
        normalized = _assert_public_video_url(candidate)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return StreamingResponse(
        _video_scan_stream(normalized),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# Media verification: metadata-only by default; never trust an extension alone.
_VIDEO_SIGNATURES = [
    (b"\x00\x00\x00", "ISO Base Media container", "mp4"),
    (b"\x1a\x45\xdf\xa3", "Matroska/WebM container", "mkv/webm"),
    (b"RIFF", "RIFF container", "avi/webp/wav"),
    (b"OggS", "Ogg container", "ogv"),
    (b"FLV", "Flash video container", "flv"),
]
def _format_bytes(value):
    if value is None or value < 0:
        return "Unknown"
    units = ("B", "KB", "MB", "GB", "TB")
    number = float(value)
    for unit in units:
        if number < 1000 or unit == units[-1]:
            return f"{int(number)} {unit}" if unit == "B" else f"{number:.2f} {unit}"
        number /= 1000
    return f"{number:.2f} TB"


def _media_magic(data, content_type=""):
    if not data:
        return None
    if data.startswith(b"\x00\x00\x00") and len(data) >= 8 and data[4:8] == b"ftyp":
        return "MP4/MOV container"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "Matroska/WebM container"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"AVI ":
        return "AVI video"
    if data.startswith(b"FLV"):
        return "Flash video"
    if data.startswith(b"OggS"):
        return "Ogg media"
    if data.startswith(b"#EXTM3U"):
        return "HLS playlist (not a video file)"
    if b"<MPD" in data[:256] or b":MPD" in data[:256]:
        return "MPEG-DASH manifest (not a video file)"
    if data.startswith((b"<!DOCTYPE", b"<html", b"<?xml")) or b"<html" in data[:256].lower():
        return "HTML/text response (not a video file)"
    if content_type.startswith("video/"):
        return content_type
    return None


@app.get("/api/video-info", include_in_schema=False)
async def video_info(url: str = Query(..., min_length=4, max_length=4096)) -> dict[str, Any]:
    candidate = url.strip()
    if "://" not in candidate:
        candidate = "https://" + candidate
    try:
        normalized = _assert_public_video_url(candidate)
        final_url, content_type, body, status = await asyncio.to_thread(_safe_video_fetch, normalized, 4096, "GET")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except RuntimeError:
        raise HTTPException(status_code=502, detail="The media host could not be reached.") from None
    if status >= 400:
        raise HTTPException(status_code=502, detail=f"Media host returned HTTP {status}.")
    magic = _media_magic(body, content_type)
    # Content-Range provides the full size when the host honors a byte-range request.
    total_size = None
    try:
        request = urllib.request.Request(final_url, headers={
            "User-Agent": "OpenFind-VideoScout/1.0",
            "Range": "bytes=0-0",
            "Accept": "*/*",
        })
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=5) as response:
            content_range = response.headers.get("Content-Range", "")
            match = re.search(r"/(\d+)$", content_range)
            if match:
                total_size = int(match.group(1))
            elif response.headers.get("Content-Length") and response.status == 200:
                total_size = int(response.headers["Content-Length"])
    except (OSError, ValueError, urllib.error.URLError):
        pass
    if total_size is None:
        try:
            length = urllib.request.Request(final_url, method="HEAD", headers={"User-Agent":"OpenFind-VideoScout/1.0"})
            with urllib.request.build_opener(_NoRedirect()).open(length, timeout=5) as response:
                if response.headers.get("Content-Length"):
                    total_size = int(response.headers["Content-Length"])
        except (OSError, ValueError, urllib.error.URLError):
            pass
    parsed = urllib.parse.urlsplit(final_url)
    return {
        "url": final_url,
        "name": urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1]) or "Media without filename",
        "content_type": content_type or "unknown",
        "size_bytes": total_size,
        "size": _format_bytes(total_size),
        "verified_format": magic or "Unverified: host did not expose recognizable media bytes",
        "is_video_candidate": bool(magic and not magic.startswith(("HTML/", "HLS ", "MPEG-DASH"))),
        "tiny_file_warning": total_size is not None and total_size < 10_000,
        "note": "Size and format depend on what the public host exposes. A short or blocked response is not proof of a valid complete video.",
    }



# Extra engines adapted from the open-source ReClip pattern and public TeraBox share API examples.
_GALLERY_DL_HOSTS = {
    "instagram.com", "redd.it", "reddit.com", "x.com", "twitter.com", "tumblr.com",
    "pixiv.net", "flickr.com", "imgur.com", "deviantart.com", "bsky.app",
}
_TERABOX_HOSTS = {
    "terabox.com", "terabox.app", "1024terabox.com", "teraboxshare.com", "teraboxlink.com",
}
_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".flv", ".ogv", ".3gp", ".ts", ".mts", ".m2ts"}


def _completed_video_files(directory: str | Path, *, recursive: bool = False) -> list[Path]:
    """Return actual video outputs, excluding subtitles, thumbnails, and audio-only files."""
    root = Path(directory)
    candidates = root.rglob("*") if recursive else root.iterdir()
    output: list[Path] = []
    for path in candidates:
        try:
            if path.is_file() and path.suffix.lower() in _VIDEO_EXTENSIONS and path.stat().st_size > 10_000:
                output.append(path)
        except OSError:
            continue
    return output


def _host_is(host: str, domains: set[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _terabox_share_code(url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlsplit(url)
    match = re.search(r"/(?:s|sharing/link)/([^/?#]+)", parsed.path, re.I)
    if not match:
        raise ValueError("Paste a TeraBox public share link, not a homepage or search page.")
    password = urllib.parse.parse_qs(parsed.query).get("pwd", [""])[0][:16]
    return match.group(1), password


def _terabox_api_json(endpoint: str, *, params: dict[str, str] | None = None, body: dict[str, str] | None = None) -> dict[str, Any]:
    api_url = "https://terabox-dl.qtcloud.workers.dev/api/" + endpoint
    if params:
        api_url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(api_url, data=data, method="POST" if data is not None else "GET", headers={
        "User-Agent": "OpenFind/1.2", "Accept": "application/json", "Content-Type": "application/json",
    })
    with urllib.request.urlopen(request, timeout=18) as response:
        payload = json.loads(response.read(2_000_000).decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("ok") is False:
        message = payload.get("message") if isinstance(payload, dict) else None
        raise RuntimeError(_clean_text(message or "TeraBox share API did not return a file.", 240))
    return payload


def _terabox_video_info(url: str) -> dict[str, Any]:
    share_code, password = _terabox_share_code(url)
    info = _terabox_api_json("get-info", params={"shorturl": share_code, "pwd": password})
    candidates: list[dict[str, Any]] = []
    def visit(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("is_dir", "0")) == "1":
                visit(item.get("children"))
            elif item.get("fs_id") is not None:
                candidates.append(item)
    visit(info.get("list"))
    videos = [item for item in candidates if Path(str(item.get("filename") or "")).suffix.lower() in _VIDEO_EXTENSIONS]
    selected = videos[0] if videos else (candidates[0] if candidates else None)
    if not selected:
        raise RuntimeError("No downloadable file was found in this TeraBox share.")
    return {
        "title": _clean_text(selected.get("filename") or "TeraBox file", 250),
        "webpage_url": url, "extractor": "TeraBox",
        "ext": Path(str(selected.get("filename") or "")).suffix.lstrip(".").lower() or "unknown",
        "size_bytes": int(selected["size"]) if str(selected.get("size", "")).isdigit() else None,
        "_terabox": {key: str(info.get(key, "")) for key in ("shareid", "uk", "sign", "timestamp")} | {
            "fs_id": str(selected.get("fs_id", "")), "pwd": password, "share_code": share_code,
        },
    }


def _terabox_download_redirect(url: str) -> str:
    metadata = _terabox_video_info(url)
    fields = metadata["_terabox"]
    result = _terabox_api_json("get-download", body={key: fields[key] for key in ("shareid", "uk", "sign", "timestamp", "fs_id")})
    download_url = _safe_http_url(result.get("downloadLink"))
    if not download_url:
        raise RuntimeError("TeraBox did not return a valid public download URL.")
    _assert_public_video_url(download_url)
    return download_url


def _gallery_dl_probe(url: str) -> dict[str, Any] | None:
    host = (urlparse(url).hostname or "").lower()
    if not _host_is(host, _GALLERY_DL_HOSTS):
        return None
    try:
        probe = subprocess.run(["gallery-dl", "--simulate", "--dump-json", url], capture_output=True, text=True, timeout=22, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if probe.returncode != 0:
        return None
    title = Path(urllib.parse.unquote(urlparse(url).path.rstrip("/").split("/")[-1] or "social-media-video")).stem
    title = re.sub(r"[-_]+", " ", title).strip() or host
    return {
        "title": _clean_text(title, 250), "webpage_url": url, "extractor": "gallery-dl",
        "ext": "unknown", "size_bytes": None, "size": "Unknown", "duration": None,
        "thumbnail": None, "download_url": "/api/video-download?url=" + urllib.parse.quote(url, safe=""),
        "preview_url": None, "note": "Secondary extractor selected; the post must expose a downloadable video.",
    }


def _gallery_dl_download(url: str, output_dir: str) -> Path:
    host = (urlparse(url).hostname or "").lower()
    if not _host_is(host, _GALLERY_DL_HOSTS):
        raise RuntimeError("No alternate extractor is configured for this site.")
    result = subprocess.run(["gallery-dl", "--destination", output_dir, url], capture_output=True, text=True, timeout=240, check=False)
    if result.returncode != 0:
        lines = (result.stderr or result.stdout or "gallery-dl could not extract this post.").strip().splitlines()
        raise RuntimeError(_clean_text(lines[-1] if lines else "gallery-dl failed.", 240))
    files = _completed_video_files(output_dir, recursive=True)
    if not files:
        raise RuntimeError("The alternate extractor did not find a completed video file in this post.")
    return max(files, key=lambda p: p.stat().st_size)


def _video_ydl_options(*, skip_download: bool, output_template: str | None = None) -> dict[str, Any]:
    """Configure yt-dlp according to the tools actually present on this server."""
    deno_bin = Path.home() / ".deno" / "bin"
    if deno_bin.is_dir():
        current_path = os.environ.get("PATH", "")
        if str(deno_bin) not in current_path.split(os.pathsep):
            os.environ["PATH"] = str(deno_bin) + os.pathsep + current_path

    ffmpeg_available = shutil.which("ffmpeg") is not None
    js_runtime = next(
        (name for name in ("deno", "node", "bun", "quickjs") if shutil.which(name)),
        None,
    )
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": skip_download,
        "socket_timeout": 25,
        "extractor_retries": 2,
        "retries": 3,
        "fragment_retries": 3,
        # With FFmpeg, separate high-quality video/audio streams can be merged.
        # Without it, prefer a single muxed format rather than guaranteeing a merge failure.
        "format": "bv*+ba/b" if ffmpeg_available else "best[ext=mp4]/best",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        },
    }
    if js_runtime:
        options["js_runtimes"] = {js_runtime: {}}

    cookie_file = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    if cookie_file and Path(cookie_file).is_file():
        options["cookiefile"] = cookie_file
    if output_template:
        options["outtmpl"] = output_template
        options["restrictfilenames"] = True
        if ffmpeg_available:
            options["merge_output_format"] = "mp4"
            options["recodevideo"] = "mp4"
    return options


def _quick_video_html_fallback(target: str) -> dict[str, Any] | None:
    """Find explicitly exposed media URLs when a site's yt-dlp extractor is unavailable.

    This deliberately inspects only the public page and its inline HTML/JSON/JS/CSS.
    It does not execute scripts, crawl subpages, or bypass authentication/DRM.
    """
    try:
        final_url, content_type, body, status = _safe_video_fetch(
            target, MAX_VIDEO_HTML_BYTES, "GET"
        )
    except (RuntimeError, ValueError):
        return None
    if status >= 400 or not ("html" in content_type.lower() or content_type.lower().startswith("text/")):
        return None

    source = body.decode("utf-8", errors="replace")
    parser, _, _, _ = _parse_video_html(source, final_url)
    candidates = list(parser.media_urls) + _video_candidates(source, final_url)
    for blob in parser.inline_scripts + parser.inline_styles:
        candidates.extend(_video_candidates(blob, final_url))

    page_title = _clean_text(" ".join(parser.title_parts).strip(), 180)
    for raw in list(dict.fromkeys(candidates))[:MAX_VIDEO_ITEMS]:
        value = _resolve_video_url(raw, final_url)
        kind = _video_kind(value or "")
        if not value or not kind:
            continue
        try:
            value = _assert_public_video_url(value)
        except ValueError:
            continue
        item = _video_item(value, kind)
        extension = item["extension"].lstrip(".") or "unknown"
        return {
            "title": item["title"] or page_title or "Video",
            "webpage_url": target,
            "extractor": "HTML media fallback",
            "ext": extension,
            "size_bytes": None,
            "size": "Unknown",
            "duration": None,
            "thumbnail": None,
            "download_url": "/api/video-download?url=" + urllib.parse.quote(value, safe=""),
            "preview_url": value if item["stream"] == "false" else None,
            "note": "Found a public media URL in page HTML. Sites requiring sign-in, special request headers, or protected playback may still block the download.",
        }
    return None


def _video_extraction_error(exc: Exception) -> tuple[int, str]:
    message = str(exc).lower()
    if any(term in message for term in ("sign in to confirm", "not a bot", "confirm you're not", "confirm you’re not", "login required", "authentication required", "cookies")):
        return 422, (
            "The platform requires sign-in or rejected this cloud server as automated traffic. "
            "Try a publicly accessible video, or configure your own authorized browser-exported cookies "
            "as a private YTDLP_COOKIES_FILE on the server. Private or protected videos are not supported."
        )
    if "unsupported url" in message or "no suitable extractor" in message:
        return 422, "This website or link type is not supported by the installed video extractor. Try the direct post or video URL."
    if "ffmpeg" in message:
        return 503, "The server could not process this video because FFmpeg is unavailable. Check the latest deployment logs."
    if "timed out" in message or "timeout" in message:
        return 504, "The video host took too long to respond. Try again later."
    return 422, "The site did not provide a downloadable video. It may require sign-in, block cloud servers, or use protected playback."


@app.get("/api/video-resolve", include_in_schema=False)
async def video_resolve(url: str = Query(..., min_length=4, max_length=2048)) -> dict[str, Any]:
    """Resolve one video page with yt-dlp's maintained site-specific extractors."""
    candidate = url.strip()
    if "://" not in candidate:
        candidate = "https://" + candidate
    try:
        normalized = _assert_public_video_url(candidate)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    direct_ext = Path(urlparse(normalized).path).suffix.lower()
    if direct_ext in _VIDEO_EXTENSIONS:
        filename = Path(urllib.parse.unquote(urlparse(normalized).path)).name or "video" + direct_ext
        return {
            "title": _clean_text(Path(filename).stem or "Video", 250),
            "webpage_url": normalized, "extractor": "Direct media URL",
            "ext": direct_ext.lstrip("."), "size_bytes": None, "size": "Unknown",
            "duration": None, "thumbnail": None,
            "download_url": "/api/video-download?url=" + urllib.parse.quote(normalized, safe=""),
            "preview_url": normalized,
            "note": "Direct video file URL.",
        }

    if _host_is(urlparse(normalized).hostname or "", _TERABOX_HOSTS):
        try:
            metadata = await asyncio.to_thread(_terabox_video_info, normalized)
            metadata["size"] = _format_bytes(metadata["size_bytes"]) if metadata["size_bytes"] is not None else "Unknown"
            metadata["download_url"] = "/api/video-download?url=" + urllib.parse.quote(normalized, safe="")
            metadata["preview_url"] = None
            metadata["note"] = "TeraBox public-share links use an external resolver; only download files you are authorized to access."
            metadata.pop("_terabox", None)
            return metadata
        except Exception as exc:
            logger.info("TeraBox resolve failed (%s): %s", type(exc).__name__, str(exc)[:200])
            raise HTTPException(status_code=422, detail="TeraBox could not resolve this public share. Check the link, password, and provider availability.") from None

    def extract() -> dict[str, Any]:
        try:
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError("Video engine is not installed yet.") from exc
        options = _video_ydl_options(skip_download=True)
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(normalized, download=False)
        if not info:
            raise RuntimeError("No video information was returned.")
        if info.get("entries") is not None:
            entries = [entry for entry in info["entries"] if entry]
            if not entries:
                raise RuntimeError("No single video was found at this link.")
            info = entries[0]

        # Use the extractor's selected result, not a guessed raw stream URL.
        selected_formats = info.get("requested_formats") or []
        size = info.get("filesize") or info.get("filesize_approx")
        if size is None and selected_formats:
            known_sizes = [f.get("filesize") or f.get("filesize_approx") for f in selected_formats]
            if all(value is not None for value in known_sizes):
                size = sum(int(value) for value in known_sizes)
        extension = info.get("ext") or "unknown"
        title = _clean_text(info.get("title") or info.get("id") or "Video", 250)
        return {
            "title": title,
            "webpage_url": normalized,
            "extractor": _clean_text(info.get("extractor_key") or info.get("extractor") or "Web", 100),
            "ext": _clean_text(extension, 20),
            "size_bytes": size,
            "size": _format_bytes(int(size)) if size is not None else "Unknown",
            "duration": info.get("duration"),
            "thumbnail": _safe_http_url(info.get("thumbnail")),
            "download_url": "/api/video-download?url=" + urllib.parse.quote(normalized, safe=""),
            # Do not send expiring, header-bound CDN stream URLs to the browser.
            # The server-side download endpoint selects and combines the real formats.
            "preview_url": None,
            "note": "Downloads are processed on the server; separate audio/video streams are merged when needed.",
        }

    try:
        return await asyncio.to_thread(extract)
    except HTTPException:
        raise
    except Exception as exc:
        logger.info("Video resolve failed (%s): %s", type(exc).__name__, str(exc)[:240])
        if "not installed yet" in str(exc):
            raise HTTPException(status_code=503, detail="The video engine is not installed. Check the latest deployment logs.") from None
        try:
            alternate = await asyncio.to_thread(_gallery_dl_probe, normalized)
        except Exception:
            alternate = None
        if alternate:
            return alternate
        try:
            html_fallback = await asyncio.to_thread(_quick_video_html_fallback, normalized)
        except Exception:
            html_fallback = None
        if html_fallback:
            return html_fallback
        status_code, detail = _video_extraction_error(exc)
        raise HTTPException(status_code=status_code, detail=detail) from None


MAX_DIRECT_VIDEO_BYTES = 1_000_000_000
_DIRECT_RANGE_RE = re.compile(r"^bytes=(?:\d+-\d*|-\d+)$", re.I)
_DIRECT_REDIRECT_CODES = {301, 302, 303, 307, 308}


def _open_direct_video_response(url: str, range_header: str | None = None):
    """Open a direct media URL without automatic redirects so every hop can be validated."""
    current = url
    opener = urllib.request.build_opener(_NoRedirect())
    for _ in range(4):
        safe_url = _assert_public_video_url(current)
        headers = {
            "User-Agent": "OpenFind-VideoDownloader/1.2",
            "Accept": "video/*,application/octet-stream;q=0.9,*/*;q=0.4",
            "Accept-Encoding": "identity",
        }
        if range_header and _DIRECT_RANGE_RE.fullmatch(range_header.strip()):
            headers["Range"] = range_header.strip()
        request = urllib.request.Request(safe_url, headers=headers, method="GET")
        try:
            response = opener.open(request, timeout=25)
        except urllib.error.HTTPError as exc:
            if exc.code in _DIRECT_REDIRECT_CODES:
                location = exc.headers.get("Location", "")
                exc.close()
                if not location:
                    raise RuntimeError("The media host returned an invalid redirect.") from None
                current = urllib.parse.urljoin(safe_url, location)
                continue
            status = exc.code
            exc.close()
            raise RuntimeError(f"The media host returned HTTP {status}.") from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise RuntimeError("Could not connect to the media host. The link may have expired or the host may block downloads.") from None

        headers = response.headers
        content_type = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type.startswith("text/") or content_type in {"application/json", "application/xhtml+xml"} or content_type.endswith("+json"):
            response.close()
            raise RuntimeError("The supplied link returned a webpage or error response, not a video file. Paste the original video page link instead.")
        length = headers.get("Content-Length", "")
        try:
            if length and int(length) > MAX_DIRECT_VIDEO_BYTES:
                response.close()
                raise RuntimeError("This direct video exceeds the 1 GB server download limit. Use the local downloader for large files.")
            content_range = headers.get("Content-Range", "")
            total_match = re.search(r"/(\d+)$", content_range)
            if total_match and int(total_match.group(1)) > MAX_DIRECT_VIDEO_BYTES:
                response.close()
                raise RuntimeError("This direct video exceeds the 1 GB server download limit. Use the local downloader for large files.")
        except ValueError:
            response.close()
            raise RuntimeError("The media host returned invalid file-size metadata.") from None
        return response, safe_url
    raise RuntimeError("The media host redirected too many times.")


def _iter_direct_video_response(response):
    try:
        while True:
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        response.close()


def _direct_download_headers(response, final_url: str) -> tuple[dict[str, str], str]:
    parsed = urllib.parse.urlsplit(final_url)
    filename = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1]) or "video"
    filename = re.sub(r"[\x00-\x1f\x7f\"\\/;]", "_", filename).strip(" .")[:180] or "video"
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", filename).strip(" .") or "video"
    encoded_name = urllib.parse.quote(filename, safe="")
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    media_type = content_type if content_type.startswith("video/") or content_type == "application/octet-stream" else "application/octet-stream"
    headers = {
        "Content-Disposition": f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }
    for name in ("Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag"):
        value = response.headers.get(name)
        if value:
            headers[name] = value
    return headers, media_type


@app.get("/api/video-download", include_in_schema=False)
async def video_download(request: Request, url: str = Query(..., min_length=4, max_length=2048)):
    """Download one selected video through yt-dlp; never scrape a page for unrelated videos."""
    candidate = url.strip()
    if "://" not in candidate:
        candidate = "https://" + candidate
    try:
        normalized = _assert_public_video_url(candidate)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    direct_ext = Path(urlparse(normalized).path).suffix.lower()
    if direct_ext in _VIDEO_EXTENSIONS:
        try:
            upstream, final_url = await asyncio.to_thread(
                _open_direct_video_response, normalized, request.headers.get("range")
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        headers, media_type = _direct_download_headers(upstream, final_url)
        return StreamingResponse(
            _iter_direct_video_response(upstream),
            status_code=getattr(upstream, "status", 200),
            headers=headers,
            media_type=media_type,
        )

    if _host_is(urlparse(normalized).hostname or "", _TERABOX_HOSTS):
        try:
            target = await asyncio.to_thread(_terabox_download_redirect, normalized)
            return RedirectResponse(target, status_code=302)
        except Exception as exc:
            logger.info("TeraBox download failed (%s): %s", type(exc).__name__, str(exc)[:200])
            raise HTTPException(status_code=422, detail="TeraBox could not create a download link for this public share. Check the link and try again.") from None

    temp_dir = tempfile.mkdtemp(prefix="openfind-video-")

    def download_file() -> Path:
        try:
            import yt_dlp
            template = os.path.join(temp_dir, "%(title).120B-%(id)s.%(ext)s")
            options = _video_ydl_options(skip_download=False, output_template=template)
            # Bound processing on the free web service; users can retry smaller formats if needed.
            options["max_filesize"] = 1_000_000_000
            options["overwrites"] = True
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    result = ydl.extract_info(normalized, download=True)
            except Exception:
                shutil.rmtree(temp_dir, ignore_errors=True)
                os.makedirs(temp_dir, exist_ok=True)
                return _gallery_dl_download(normalized, temp_dir)
            if result and result.get("entries") is not None:
                entries = [entry for entry in result["entries"] if entry]
                if not entries:
                    raise RuntimeError("No video was downloaded.")
            files = _completed_video_files(temp_dir)
            if not files:
                raise RuntimeError("No completed video file was produced. The extractor may have returned only audio, subtitles, or an incomplete download.")
            return max(files, key=lambda path: path.stat().st_size)
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    try:
        path = await asyncio.to_thread(download_file)
    except Exception as exc:
        logger.info("Video download failed (%s): %s", type(exc).__name__, str(exc)[:240])
        status_code, detail = _video_extraction_error(exc)
        if "not installed yet" in str(exc):
            status_code, detail = 503, "The video engine is not installed. Check the latest deployment logs."
        raise HTTPException(status_code=status_code, detail=detail) from None

    media_type = "video/mp4" if path.suffix.lower() == ".mp4" else "application/octet-stream"
    return FileResponse(
        path=str(path),
        media_type=media_type,
        filename=path.name,
        background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
    )

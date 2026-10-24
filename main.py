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
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from ddgs import DDGS
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
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

"""OpenFind: lightweight FastAPI metasearch."""
from __future__ import annotations

import ast
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

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "templates" / "index.html"
PAGE_SIZE = 10
MAX_PAGE = 10
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
    source = _clean_text(item.get("source") or item.get("publisher") or item.get("provider"), 150)
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
    unique = list(dict.fromkeys(x.strip() for x in options if x.strip()))
    return JSONResponse({"suggestions": unique[:5]})


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
        try:
            raw = await asyncio.to_thread(_search_sync, query, category, page, safesearch)
        except Exception as exc:
            logger.warning("Metasearch provider failure: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Search is temporarily unavailable. Please try again shortly.") from None
    raw = raw or []
    has_next = len(raw) > PAGE_SIZE
    results = []
    for item in raw[:PAGE_SIZE]:
        if isinstance(item, dict):
            normalized = _normalize_result(item, category)
            if normalized:
                results.append(normalized)
    return JSONResponse({
        "query": query, "category": category, "page": page, "page_size": PAGE_SIZE,
        "result_count": len(results), "has_next": has_next, "results": results,
        "instant_answer": _instant_answer(query),
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), workers=1)

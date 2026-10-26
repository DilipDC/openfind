# OpenFind

OpenFind is a lightweight open-source metasearch web application built with FastAPI, Uvicorn, DDGS, Tailwind CSS and vanilla JavaScript. It can be deployed as a Render web service and needs no paid search API key or database.

## Features

- Web, news, image and video search via the open-source DDGS metasearch library.
- Debounced local autocomplete; typed prefixes are not sent to search providers for suggestions.
- Safe arithmetic instant-answer card implemented using a restricted Python AST, not eval().
- Pagination, safe-search settings, dark/light mode and JSON result export.
- Responsive single-page UI; no login, analytics SDK, saved query history or app database.
- Input validation, URL normalization, bounded concurrent upstream calls and generic provider error messages.
- Health check endpoint at /health and optional API docs controlled by ENABLE_API_DOCS.

## Local run

Requires Python 3.13.4 or a compatible supported Python release and Git.

### Windows PowerShell

    py -3.13 -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install --upgrade pip
    pip install -r requirements.txt
    $env:PORT = "8000"
    uvicorn main:app --host 0.0.0.0 --port $env:PORT --workers 1

### macOS / Linux

    python3.13 -m venv .venv
    source .venv/bin/activate
    python -m pip install --upgrade pip
    pip install -r requirements.txt
    export PORT=8000
    uvicorn main:app --host 0.0.0.0 --port "$PORT" --workers 1

Open http://127.0.0.1:8000. Check http://127.0.0.1:8000/health.

## Tests

    pip install -r requirements-dev.txt
    pytest -q

Tests mock provider responses so they do not need live search backends.

## Deploy on Render

1. Push this repository to GitHub.
2. In Render, select New, then Blueprint, and connect this repository. Render reads render.yaml, or choose New Web Service and use the settings below.
3. Build command: pip install -r requirements.txt
4. Start command: uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers
5. Select the Free plan in the Singapore region and wait for the deploy to become live.
6. Visit the service URL and confirm that /health returns {"status":"ok","service":"openfind"}.

Render Free services sleep after inactivity and have cold-start latency. Free hosting is suitable for testing/hobby projects and does not provide an always-on production SLA. Search providers can rate-limit requests or change behavior.

## Git commands

If using the GitHub repository already created at https://github.com/DilipDC/openfind:

    git clone https://github.com/DilipDC/openfind.git
    cd openfind
    # copy the project files into this directory
    git add .
    git commit -m "Add OpenFind metasearch app"
    git push origin main

For a fresh repository instead:

    git init
    git add .
    git commit -m "Initial OpenFind metasearch app"
    git branch -M main
    git remote add origin https://github.com/DilipDC/openfind.git
    git push -u origin main

## Privacy and limitations

OpenFind does not persist queries in an application search-history database or log query text intentionally. Queries are still sent to upstream providers by DDGS; hosting providers, upstream providers and destination websites have their own logging and privacy policies. Image previews may make requests to external hosts. OpenFind does not promise anonymity.

DDGS is third-party software and provider access can break or be rate-limited. Review upstream terms, hosting logs, per-client rate limiting, abuse controls and uptime requirements before putting this service under heavy or critical production traffic.

## Configuration

- PORT: Render-provided HTTP port; defaults to 8000 locally.
- ENABLE_API_DOCS: set to true to enable /docs; defaults to false.
- LOG_LEVEL: Python log level; defaults to WARNING.
- PYTHON_VERSION: Render runtime is pinned by the Blueprint to 3.13.4.

## License

MIT. See LICENSE.


## OpenFind 1.1 additions

- Spelling suggestions powered by the MIT-licensed pyspellchecker project: https://github.com/barrust/pyspellchecker. Corrections are suggestions; searches are not silently rewritten.
- Passive OSINT for public domains and global IP addresses: public DNS A/AAAA/MX/NS records, RDAP registration data where available, and links to VirusTotal, urlscan.io, and Certificate Transparency. It does not scan ports or attempt access.
- Separate public onion-index search via Ahmia: https://ahmia.fi/about/. The Onion index button opens Ahmia in a separate tab; this app does not run Tor, crawl onion sites, or guarantee indexed links are safe.
- 20 results per page, up to 25 pages.
- Optional SearXNG integration: set the SEARXNG_URL environment variable to an instance you operate or trust. It uses the instance JSON search API; if unavailable or disabled, search falls back to DDGS. Many SearXNG instances disable JSON by default. Docs: https://docs.searxng.org/dev/search_api.html

Open-source components reused: DDGS (https://github.com/deedy5/ddgs), pyspellchecker (https://github.com/barrust/pyspellchecker), and the SearXNG HTTP API. OpenFind integrates their published library/API; it does not copy their code.



## Video Scout

Open the /video page from the OpenFind navigation to scan a public webpage for direct video URLs. The bounded scanner checks exposed HTML media attributes, embedded JavaScript, linked JavaScript/CSS, selected inline styles/scripts, and up to four same-origin subpages. It streams NDJSON events so found videos and scan progress appear as the scan runs.

- Supported direct media extensions include MP4, M4V, WebM, MOV, MKV, AVI, FLV, OGV, and 3GP; HLS .m3u8 and MPEG-DASH .mpd manifests are labelled separately.
- Video links open the discovered source directly; browser/server download behavior depends on the source site's headers and permissions. HLS/DASH manifests are not merged into a file.
- The scan is intentionally capped by response size and counts of pages, scripts, stylesheets and results. It does not execute JavaScript, use logged-in cookies, crawl the whole site, bypass DRM, fetch onion sites, or access private/local addresses. Runtime-created blob URLs and login-gated players may not be detected.
- Server-side scanning validates public DNS/IP addresses and re-validates every redirect. Public services should still use rate limits and abuse monitoring before scaling.

The Video Scout is part of OpenFind and deploys with the existing Render web service when updates to main are pushed.

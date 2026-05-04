"""Minimal search + page-fetch service for the browser.* verl tools.

Contract (matches verl_rl_ehr/tools/browser_*_tool.py in BROWSER_SEARCH_MODE=http):

  POST /search   {"query": str, "topn": int}   → plain-text search results
  POST /open     {"id": str|int, "cursor": int, "loc": int,
                  "num_lines": int, "view_source": bool, "source": str, "url": str}
                 → plain-text page contents (optionally sliced by loc/num_lines)
  POST /find     {"pattern": str, "cursor": int}
                 → plain-text "line N: …" matches within the last-opened page

State (in-process, per-trajectory via `cursor` int):
  - A small LRU of opened pages keyed by cursor.
  - Each rollout is implicitly single-cursor: when you call /search we
    remember the top-N URLs keyed by their integer id (0..N-1). When you
    call /open with `id=<int>` we fetch that URL, store it at the next free
    cursor, and return it. Then /find searches inside the most recent page.

Run:
  SERPER_API_KEY=... python -m verl_rl_ehr.services.search_service --port 8090

Dependencies (already in venvs/qwen3_5_rl): fastapi, uvicorn, httpx.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
from collections import OrderedDict
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


logger = logging.getLogger("ehr_search_service")
logging.basicConfig(level=logging.INFO)


SERPER_SEARCH_URL = "https://google.serper.dev/search"
SERPER_SCRAPE_URL = "https://scrape.serper.dev/"

_LINK_CACHE_MAX = 256
# id -> url, LRU. Populated by /search so /open can resolve integer ids.
_link_cache: OrderedDict[int, str] = OrderedDict()
_cursor_cache: OrderedDict[int, str] = OrderedDict()  # cursor -> page text
_last_cursor = -1


class SearchReq(BaseModel):
    query: str
    topn: int = 10


class OpenReq(BaseModel):
    id: Optional[Any] = -1
    cursor: int = -1
    loc: int = -1
    num_lines: int = -1
    view_source: bool = False
    source: Optional[str] = None
    url: Optional[str] = None


class FindReq(BaseModel):
    pattern: str
    cursor: int = -1


app = FastAPI(title="EHR browser service")


async def _serper_search(query: str, topn: int) -> list[dict]:
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        raise HTTPException(500, "SERPER_API_KEY not set on server")
    payload = {"q": query, "num": min(max(1, topn), 20)}
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(SERPER_SEARCH_URL, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
    return data.get("organic", []) or []


async def _serper_scrape(url: str) -> str:
    """Fetch text body of a URL via Serper's scrape endpoint (falls back to
    direct GET if scrape fails)."""
    api_key = os.environ.get("SERPER_API_KEY")
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"} if api_key else {}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        if api_key:
            try:
                r = await client.post(SERPER_SCRAPE_URL, json={"url": url}, headers=headers)
                r.raise_for_status()
                data = r.json()
                text = data.get("text") or data.get("markdown") or ""
                if text:
                    return text
            except Exception as e:  # noqa: BLE001
                logger.warning("scrape failed for %s: %s; falling back to direct GET", url, e)
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (ehr-rl)"})
        r.raise_for_status()
        return r.text


def _slice_page(page: str, loc: int, num_lines: int) -> str:
    lines = page.splitlines()
    start = 0 if loc < 0 else max(0, loc)
    n = 100 if num_lines <= 0 else num_lines
    end = min(len(lines), start + n)
    sliced = lines[start:end]
    total = len(lines)
    header = f"[page lines {start}..{end} of {total}]\n"
    return header + "\n".join(sliced)


@app.post("/search")
async def search(req: SearchReq):
    query = req.query.strip()
    if not query:
        return "Error: empty query"
    try:
        results = await _serper_search(query, req.topn)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        return f"Search failed: {e}"
    if not results:
        return f"No results for: {query}"
    out_lines = [f"Search results for: {query}"]
    for i, r in enumerate(results[: req.topn]):
        _link_cache[i] = r.get("link", "")
        # LRU trim
        while len(_link_cache) > _LINK_CACHE_MAX:
            _link_cache.popitem(last=False)
        title = r.get("title", "")
        snippet = r.get("snippet", "")
        link = r.get("link", "")
        out_lines.append(f"  [{i}] {title}")
        out_lines.append(f"      {link}")
        if snippet:
            out_lines.append(f"      {snippet}")
    return "\n".join(out_lines)


@app.post("/open")
async def open_(req: OpenReq):
    global _last_cursor
    # Resolve id → url. Model sends id=int (from search) or id=str (URL) or url=...
    url = req.url
    if not url:
        rid = req.id
        if isinstance(rid, int) or (isinstance(rid, str) and rid.isdigit()):
            url = _link_cache.get(int(rid))
        elif isinstance(rid, str) and rid.startswith(("http://", "https://")):
            url = rid
    if not url:
        return f"Error: cannot resolve link id={req.id!r} (no prior search result or URL)"
    try:
        text = await _serper_scrape(url)
    except Exception as e:  # noqa: BLE001
        return f"Open failed: {e}"
    _last_cursor += 1
    cursor = _last_cursor
    _cursor_cache[cursor] = text
    while len(_cursor_cache) > _LINK_CACHE_MAX:
        _cursor_cache.popitem(last=False)
    header = f"[opened {url} -> cursor={cursor}]\n"
    return header + _slice_page(text, req.loc, req.num_lines)


@app.post("/find")
async def find(req: FindReq):
    cursor = req.cursor if req.cursor >= 0 else _last_cursor
    page = _cursor_cache.get(cursor)
    if page is None:
        return f"Error: no page at cursor={cursor}. Call /open first."
    pattern = req.pattern.strip()
    if not pattern:
        return "Error: empty pattern"
    try:
        rgx = re.compile(re.escape(pattern), re.IGNORECASE)
    except re.error as e:
        return f"Error: bad pattern: {e}"
    hits: list[str] = []
    for i, line in enumerate(page.splitlines()):
        if rgx.search(line):
            hits.append(f"line {i}: {line.strip()[:300]}")
            if len(hits) >= 20:
                break
    if not hits:
        return f"No matches for {pattern!r} in cursor={cursor}"
    return "\n".join(hits)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "serper_key_set": bool(os.environ.get("SERPER_API_KEY"))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    args = ap.parse_args()
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

"""Oxylabs AI Studio — web search + extract provider (plugin form).

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. Search is backed
by Oxylabs AI-Search and extract by AI-Scraper (markdown), via the optional
``oxylabs-ai-studio`` package. The API key is read from ``OXYLABS_API_KEY``
(mirrors the FIRECRAWL_API_KEY convention); for Hosted Agents it is injected
per-turn from the agent's own (BYOK) key.

Both calls normalize defensively: an unexpected SDK shape or a missing key is
returned as ``{"success": False, "error": ...}`` (search) or a per-URL ``error``
row (extract) rather than raising into the turn.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)


def _api_key() -> str:
    return os.getenv("OXYLABS_API_KEY", "").strip()


class OxylabsWebSearchProvider(WebSearchProvider):
    """Oxylabs AI Studio (AI-Search + AI-Scraper)."""

    @property
    def name(self) -> str:
        return "oxylabs"

    @property
    def display_name(self) -> str:
        return "Oxylabs AI Studio"

    def is_available(self) -> bool:
        """True when the ``oxylabs-ai-studio`` package is importable. No network I/O;
        the key is checked at call time so the row still registers when unset."""
        try:
            import oxylabs_ai_studio  # noqa: F401

            return True
        except ImportError:
            return False

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        key = _api_key()
        if not key:
            return {"success": False, "error": "OXYLABS_API_KEY is not set."}
        try:
            from oxylabs_ai_studio.apps.ai_search import AiSearch
        except ImportError:
            return {"success": False, "error": "oxylabs-ai-studio is not installed — `pip install oxylabs-ai-studio`."}

        safe_limit = max(1, int(limit))
        try:
            result = AiSearch(api_key=key).search(
                query=query, limit=safe_limit, render_javascript=False, return_content=True
            )
        except Exception as exc:  # noqa: BLE001 — SDK raises its own exceptions
            logger.warning("Oxylabs search error: %s", exc)
            return {"success": False, "error": f"Oxylabs search failed: {exc}"}

        web = []
        for i, hit in enumerate(_as_items(result)):
            url = str(hit.get("url") or hit.get("link") or hit.get("href") or "")
            web.append(
                {
                    "title": str(hit.get("title") or hit.get("name") or ""),
                    "url": url,
                    "description": str(hit.get("content") or hit.get("description") or hit.get("snippet") or ""),
                    "position": i + 1,
                }
            )
        logger.info("Oxylabs search '%s': %d results (limit %d)", query, len(web), limit)
        return {"success": True, "data": {"web": web}}

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        key = _api_key()
        try:
            from oxylabs_ai_studio.apps.ai_scraper import AiScraper
        except ImportError:
            return [{"url": u, "error": "oxylabs-ai-studio is not installed."} for u in urls]
        if not key:
            return [{"url": u, "error": "OXYLABS_API_KEY is not set."} for u in urls]

        # In-provider SSRF re-check (H41 defense in depth — the web_extract_tool
        # dispatcher gate is the primary check; this holds if called directly).
        from tools.url_safety import is_safe_url

        scraper = AiScraper(api_key=key)
        out: List[Dict[str, Any]] = []
        for url in urls:
            if not is_safe_url(url):
                out.append({"url": url, "error": "Blocked: URL targets a private or internal network address"})
                continue
            try:
                res = scraper.scrape(url=url, output_format="markdown", render_javascript="auto")
                content = _scrape_content(res)
                out.append(
                    {
                        "url": url,
                        "title": str(_scrape_field(res, "title") or ""),
                        "content": content,
                        "raw_content": content,
                        "metadata": {},
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Oxylabs extract error for %s: %s", url, exc)
                out.append({"url": url, "error": f"Oxylabs extract failed: {exc}"})
        return out

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Oxylabs AI Studio",
            "badge": "paid · key · search + extract",
            "tag": "AI-Search + AI-Scraper (markdown). Bring your Oxylabs AI Studio API key.",
            "env_vars": [
                {"key": "OXYLABS_API_KEY", "prompt": "Oxylabs AI Studio API key", "url": "https://aistudio.oxylabs.io/"},
            ],
        }


def _as_items(result: Any) -> List[Dict[str, Any]]:
    """Coerce an SDK search result into a list of dict hits (defensive)."""
    if result is None:
        return []
    for attr in ("results", "data", "items", "hits"):
        val = getattr(result, attr, None) if not isinstance(result, dict) else result.get(attr)
        if isinstance(val, list):
            return [x if isinstance(x, dict) else _to_dict(x) for x in val]
    if isinstance(result, list):
        return [x if isinstance(x, dict) else _to_dict(x) for x in result]
    return []


def _to_dict(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict):
        return d
    return {"content": str(obj)}


def _scrape_content(res: Any) -> str:
    if isinstance(res, str):
        return res
    if isinstance(res, dict):
        return str(res.get("content") or res.get("markdown") or res.get("data") or "")
    for attr in ("content", "markdown", "data", "text"):
        val = getattr(res, attr, None)
        if val:
            return str(val)
    return str(res or "")


def _scrape_field(res: Any, field: str) -> Any:
    if isinstance(res, dict):
        return res.get(field)
    return getattr(res, field, None)

"""Oxylabs AI Studio web plugin — bundled, auto-loaded.

AI-Search (search) + AI-Scraper (extract) via the optional ``oxylabs-ai-studio``
package. Requires ``OXYLABS_API_KEY`` (injected per-turn from the agent's BYOK key).
"""

from __future__ import annotations

from plugins.web.oxylabs.provider import OxylabsWebSearchProvider


def register(ctx) -> None:
    """Register the Oxylabs provider with the plugin context."""
    ctx.register_web_search_provider(OxylabsWebSearchProvider())

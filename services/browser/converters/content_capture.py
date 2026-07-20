"""Page content capture — open-source fallback.

The cloud build ships a shadow-DOM-piercing serializer here that inlines
open shadow-root content into the captured HTML. The open fallback keeps
the same public name and contract but returns the plain light-DOM
serialization from ``page.content()`` — correct for the vast majority of
pages, and always safe.
"""

from __future__ import annotations

from playwright.async_api import Page


async def content_with_shadow_dom(page: Page) -> str:
    """Return the page's HTML.

    Open-build fallback: plain ``page.content()`` (no shadow-DOM
    inlining). Drop-in compatible with the cloud implementation.
    """
    return await page.content()

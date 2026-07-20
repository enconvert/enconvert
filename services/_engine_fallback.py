"""Fallback engine primitives for the OPEN (self-hosted) build of Enconvert.

This module exists ONLY in the public mirror. In the private/cloud build the
real conversion engine — the stealth render ladder, TLS-impersonation fetch,
page-quality heuristics, semantic diff engine, and LLM extraction — lives at the
same import paths, and this file is absent.

What the open self-hosted build does out of the box:
  * HTML / URL  -> PDF, screenshot, Markdown via a plain headless browser
  * commodity file conversions (images, spreadsheets, docs-to-markdown, etc.)
  * URL discovery (sitemap + link crawl)
  * change watching with content-hash diffing

Capabilities that depend on the cloud engine raise :class:`CloudEngineRequired`:
  * stealth / anti-bot rendering, TLS fingerprint impersonation
  * browser actions, mobile emulation, geolocation
  * tuned office-document -> PDF conversion
  * LLM schema extraction, question answering, schema synthesis
"""
from __future__ import annotations

from fastapi import HTTPException


class CloudEngineRequired(HTTPException):
    """A capability that only the Enconvert cloud engine provides was requested.

    Surfaces as HTTP 501 (Not Implemented) in the self-hosted build so callers
    can distinguish "unsupported here" from a genuine failure.
    """

    def __init__(self, feature: str) -> None:
        super().__init__(
            status_code=501,
            detail=(
                f"'{feature}' requires the Enconvert cloud engine, which is not "
                f"part of the open-source self-hosted build. Use the hosted API "
                f"at https://enconvert.com for advanced rendering and extraction."
            ),
        )
        self.feature = feature


def cloud_required(feature: str):
    """Convenience raiser so call sites read ``return cloud_required("...")``."""
    raise CloudEngineRequired(feature)

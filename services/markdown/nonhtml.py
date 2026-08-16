"""Markdown for a NON-HTML response body (``text/plain``, JSON).

A URL that answers with ``text/plain`` or ``application/json`` has no
article to extract. Running an HTML->Markdown converter over it is not a
degraded conversion, it is a destructive one: the HTML whitespace rules
collapse every newline, so a 99 KB ``llms.txt`` becomes one 99 KB line
(and then chunks to nothing).

This module is deliberately a LEAF — it imports only stdlib + ``yaml`` —
so every layer that needs it (the V1 ``url_markdown`` converter, the V2
page-markdown helper, ``/v2/ingest``) can share one implementation
without an import cycle through the heavier converter modules.
"""

from __future__ import annotations

import yaml

__all__ = ["code_fence", "fenced_document"]


def code_fence(text: str) -> str:
    """Backtick fence longer than any backtick run in ``text`` (min 3).

    Security-relevant, not cosmetic: the body is untrusted, so a response
    containing its own ``` sequence would otherwise break out of the
    fenced block and inject Markdown structure into the deliverable.
    """
    longest = 0
    run = 0
    for ch in text:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return "`" * max(3, longest + 1)


def fenced_document(
    body_text: str,
    *,
    language: str,
    base_url: str = "",
    content_type: str | None = None,
    frontmatter: bool = True,
) -> str:
    """The body inside a fenced block, optionally with YAML frontmatter.

    ``frontmatter`` is on for the file-download outputs (a standalone .md
    should say where it came from) and off for chunking pipelines, where
    the metadata belongs in the chunk record, not in the retrievable text.
    """
    fence = code_fence(body_text)
    block = f"{fence}{language}\n{body_text}\n{fence}\n"
    if not frontmatter:
        return block
    header = yaml.safe_dump(
        {
            "url": base_url,
            "content_type": (content_type or "").split(";")[0].strip(),
        },
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    return f"---\n{header}---\n\n{block}"

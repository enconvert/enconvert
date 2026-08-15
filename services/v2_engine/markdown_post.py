"""Markdown tidy-up applied after conversion (QA round-3 fixes C1-C4).

``markdown_prep`` fixes the DOM; this module fixes what only exists once
the text is markdown:

* C1 — structural residue: headings left with no text, links left with
  no label, and runs of horizontal rules (a page's decorative ``<hr>``
  set reads as dangling ``---`` separators once the sections between
  them were stripped).
* C2 — consecutive duplicate blocks. Responsive designs ship the same
  content twice (a desktop and a mobile copy of one button bar, one
  logo strip), and animation carousels pre-render every rotation frame
  as stacked siblings. All of them survive extraction as an immediately
  repeated block; a reader sees each ONCE.
* C3 — Private Use Area glyphs. ``markdown_prep`` removes them from the
  DOM, but the readability candidate and cached artifacts can still
  carry them, and they are never content.
* C4 — data-array truncation. A notebook page that prints a 1536-float
  embedding vector spends ~40k tokens on numbers no consumer reads: on
  the QA corpus this was 93.8% of one page's entire markdown. Runs of
  numeric literals are collapsed to a head sample plus a count, so the
  shape of the data survives and the token bill does not.

Every function is pure text-in/text-out, so the whole module is unit
testable without a render.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

__all__ = ["tidy_markdown", "truncate_data_arrays"]


# --- C1: structural residue ------------------------------------------------

# A heading marker with nothing after it.
_EMPTY_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*$")

# A link whose label is empty or whitespace: ``[](url)``, ``[ ](url)``.
# Images (``![]()``) are NOT matched — an image with no alt is still a
# rendered image.
_EMPTY_LINK_RE = re.compile(r"(?<!\!)\[[\s]*\]\([^)]*\)")

# A horizontal rule in any of markdown's spellings.
_RULE_RE = re.compile(r"^\s{0,3}(?:\*\s*\*\s*\*[\s*]*|-\s*-\s*-[\s-]*|_\s*_\s*_[\s_]*)$")

# --- C3: icon-font glyphs --------------------------------------------------

_PUA_RE = re.compile(
    "[\ue000-\uf8ff]"  # Private Use Area
    "|[\U000f0000-\U000ffffd]"  # Supplementary PUA-A
    "|[\U00100000-\U0010fffd]"  # Supplementary PUA-B
)


# --- C2: repeated blocks ---------------------------------------------------

# Longest repeated block collapsed. Twelve lines covers a logo strip
# with its caption or a duplicated card; beyond that a "repeat" is more
# likely to be genuine parallel content.
_MAX_REPEAT_BLOCK_LINES: int = 12


# --- C4: numeric arrays ----------------------------------------------------

_NUMBER = r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"

# A run of comma-separated numeric literals, possibly wrapped across
# lines. The bound is the run LENGTH, not the character count: a
# 3-element RGB tuple and a 1536-element embedding are the same shape,
# and only one of them is a token problem.
# Minimum run length that counts as a data dump rather than a tuple.
_ARRAY_MIN_VALUES: int = 64

_ARRAY_RUN_RE = re.compile(
    _NUMBER
    + r"(?:\s*,\s*"
    + _NUMBER
    + r"){"
    + str(_ARRAY_MIN_VALUES - 1)
    + r",}"
)

# Values kept at the head of a truncated run.
_ARRAY_HEAD_VALUES: int = 16


def truncate_data_arrays(markdown: str) -> str:
    """Collapse long numeric runs to a head sample plus a count (C4).

    Applies inside code fences as well as outside: the embedding dumps
    that motivated this are notebook OUTPUT cells, which convert to
    fenced blocks. The replacement keeps the run's opening values so
    the data's shape and precision stay visible.
    """
    if not markdown:
        return markdown

    def _replace(match: re.Match[str]) -> str:
        values = [item.strip() for item in match.group(0).split(",")]
        total = len(values)
        if total <= _ARRAY_HEAD_VALUES:
            return match.group(0)
        head = ", ".join(values[:_ARRAY_HEAD_VALUES])
        dropped = total - _ARRAY_HEAD_VALUES
        return f"{head}, ... [truncated {dropped} of {total} values]"

    try:
        return _ARRAY_RUN_RE.sub(_replace, markdown)
    except Exception:  # noqa: BLE001 — truncation must never break output
        logger.warning("data-array truncation failed", exc_info=True)
        return markdown


def _strip_structural_residue(lines: list[str]) -> list[str]:
    """C1 — drop empty headings/links and collapse rule runs."""
    cleaned: list[str] = []
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            cleaned.append(line)
            continue
        if in_fence:
            cleaned.append(line)
            continue

        if _EMPTY_HEADING_RE.match(line):
            continue
        if _EMPTY_LINK_RE.search(line):
            line = _EMPTY_LINK_RE.sub("", line)
            if not line.strip():
                continue
        if _RULE_RE.match(line):
            # One rule per run, and never as the document's last words.
            previous = next(
                (item for item in reversed(cleaned) if item.strip()), ""
            )
            if not previous or _RULE_RE.match(previous):
                continue
        cleaned.append(line)

    while cleaned and (
        not cleaned[-1].strip() or _RULE_RE.match(cleaned[-1])
    ):
        cleaned.pop()
    return cleaned


def _fence_mask(lines: list[str]) -> list[bool]:
    """True for every line inside (or delimiting) a fenced code block."""
    mask: list[bool] = []
    in_fence = False
    for line in lines:
        is_fence = line.strip().startswith("```")
        if is_fence:
            mask.append(True)
            in_fence = not in_fence
            continue
        mask.append(in_fence)
    return mask


def _collapse_repeated_blocks(lines: list[str]) -> list[str]:
    """C2 — keep one copy of an immediately repeated block.

    Scans for the longest block that repeats starting at the current
    position and emits it once. Code fences are never collapsed (two
    identical examples side by side are legitimate), and neither are
    table rows, where duplicate data lines can be real.
    """
    mask = _fence_mask(lines)
    total = len(lines)
    output: list[str] = []
    index = 0
    while index < total:
        collapsed = False
        upper = min(_MAX_REPEAT_BLOCK_LINES, (total - index) // 2)
        for size in range(upper, 0, -1):
            block = lines[index : index + size]
            if not any(item.strip() for item in block):
                continue
            if any(mask[index : index + size]):
                continue
            if size == 1 and block[0].lstrip().startswith("|"):
                continue
            cursor = index + size
            repeats = 1
            while (
                cursor + size <= total
                and lines[cursor : cursor + size] == block
                and not any(mask[cursor : cursor + size])
            ):
                repeats += 1
                cursor += size
            if repeats < 2:
                continue
            output.extend(block)
            index = cursor
            collapsed = True
            break
        if not collapsed:
            output.append(lines[index])
            index += 1
    return output


def tidy_markdown(markdown: str, *, truncate_arrays: bool = False) -> str:
    """Apply the post-conversion tidy pass.

    Args:
        markdown: Converted markdown for one page.
        truncate_arrays: Collapse long numeric runs (C4). The caller
            resolves the request's tri-state option before calling.

    Returns:
        The tidied markdown. Never raises: on any failure the input is
        returned unchanged.
    """
    if not markdown or not markdown.strip():
        return markdown
    try:
        text = _PUA_RE.sub("", markdown)
        lines = text.split("\n")
        lines = _strip_structural_residue(lines)
        lines = _collapse_repeated_blocks(lines)
        text = "\n".join(lines)
        # Collapse the blank-line runs the removals above leave behind.
        text = re.sub(r"\n{3,}", "\n\n", text)
        if truncate_arrays:
            text = truncate_data_arrays(text)
        return text.strip() + "\n"
    except Exception:  # noqa: BLE001 — tidy must degrade, never 500
        logger.warning("markdown tidy failed", exc_info=True)
        return markdown

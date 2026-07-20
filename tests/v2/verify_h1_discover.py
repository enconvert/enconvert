"""Sprint H.1 live verification: /v2/discover end-to-end, NO browser.

Hermetic unit coverage is tests/v2/test_discover.py (pytest, 22 checks).
This harness drives the REAL pipeline — Crawl4AI HTTP-only crawler +
live sitemap/robots fetches — against three representative sites and
proves the H.1 verification items that need the world:

  (a) Static multi-page site (crawl/hybrid) returns many URLs.
  (b) Sitemap mode returns the published sitemap's entries.
  (c) JS-SPA (crawl) returns near-zero — HTTP-only cannot see
      JS-injected routes (intended behaviour, documented in
      services/v2_engine/discover_flow.py).
  (e) `ps` shows ZERO new Chromium/headless_shell processes across every
      discover call — the endpoint never spawns a browser.

Items (c robots) and (d same_domain) of the plan's verification list are
proven hermetically in test_discover.py (more deterministic than a live
site). respect_robots is additionally exercised live below.

Usage (from the gateway root):
    .venv/bin/python tests/v2/verify_h1_discover.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

GATEWAY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GATEWAY_ROOT))

from dotenv import load_dotenv

load_dotenv(GATEWAY_ROOT / ".env")

from api.v2.schemas.discover import DiscoverRequest  # noqa: E402
from services.v2_engine import discover_flow  # noqa: E402

STATIC_SITE = "https://quotes.toscrape.com/"  # static, ~50 internal links
SPA_SITE = "https://excalidraw.com/"  # pure CSR React shell, 0 <a href>
SITEMAP_SITE = "https://www.djangoproject.com/"  # serves a real /sitemap.xml

USER = {"id": "0", "key_type": "private", "subscription": {"plan_slug": "admin"}}

PERF_PATH = GATEWAY_ROOT / "tests" / "v2" / "fixtures" / "perf_H1.txt"


def chromium_count() -> int:
    """Count Playwright-launched browser processes ONLY.

    Scoped to ``ms-playwright`` / ``headless_shell`` in the process
    command line — that is the browser crawl4ai/Playwright would spawn,
    and it excludes the developer's everyday Google Chrome (whose helper
    processes churn by +-2 over a 30 s window and would otherwise create
    false positives). A discover call must keep this at 0.
    """
    out = subprocess.run(
        [
            "bash",
            "-c",
            "ps -A -o command= | grep -iE 'ms-playwright|headless_shell' "
            "| grep -v grep | wc -l",
        ],
        capture_output=True,
        text=True,
    )
    return int(out.stdout.strip() or "0")


async def _discover(label: str, **kwargs) -> tuple[str, object, float, int]:
    req = DiscoverRequest(**kwargs)
    before = chromium_count()
    start = time.monotonic()
    resp = await discover_flow.run(req, USER)
    elapsed = time.monotonic() - start
    after = chromium_count()
    delta = after - before
    return label, resp, elapsed, delta


async def main() -> int:
    results: list[str] = []
    failures: list[str] = []
    baseline = chromium_count()
    print(f"[ps] baseline chromium/headless_shell processes: {baseline}\n")

    cases = [
        ("static-crawl", dict(url=STATIC_SITE, mode="crawl", max_urls=80, max_depth=2)),
        ("static-hybrid", dict(url=STATIC_SITE, mode="hybrid", max_urls=80, max_depth=2)),
        ("spa-crawl", dict(url=SPA_SITE, mode="crawl", max_urls=50, max_depth=2)),
        ("sitemap", dict(url=SITEMAP_SITE, mode="sitemap", max_urls=200)),
        ("robots-on", dict(url=STATIC_SITE, mode="crawl", max_urls=40, respect_robots=True)),
    ]

    measured: dict[str, object] = {}
    for label, kwargs in cases:
        try:
            label, resp, elapsed, delta = await _discover(label, **kwargs)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{label}: raised {exc!r}")
            print(f"  {label}: ERROR {exc!r}")
            continue
        measured[label] = resp
        line = (
            f"  {label:14s} mode={resp.mode:7s} total={resp.total:4d} "
            f"pages_crawled={resp.pages_crawled:3d} truncated={resp.truncated} "
            f"chromium_delta={delta} {elapsed:6.2f}s"
        )
        print(line)
        results.append(line.strip())
        if resp.warnings:
            print(f"       warnings: {resp.warnings}")
        # (e) HARD GATE: no new browser process for ANY call.
        if delta != 0:
            failures.append(
                f"{label}: spawned {delta} Chromium process(es) — discover must be browser-free"
            )

    print()
    final = chromium_count()
    if final != baseline:
        failures.append(
            f"net chromium drift {baseline} -> {final}: a browser was created somewhere"
        )

    # (a) static site returns many URLs.
    static = measured.get("static-crawl")
    if static is None or static.total < 5:
        failures.append("(a) static-crawl returned < 5 URLs (expected the full site)")
    else:
        print(f"[a] static-crawl returned {static.total} URLs (>=5) PASS")

    # (b) sitemap mode returns the sitemap entries.
    sitemap = measured.get("sitemap")
    if sitemap is None or sitemap.total < 1:
        failures.append("(b) sitemap mode returned 0 URLs (expected sitemap entries)")
    else:
        print(f"[b] sitemap mode returned {sitemap.total} URLs (>=1) PASS")

    # (c) SPA crawl returns near-zero AND fewer than the static site.
    spa = measured.get("spa-crawl")
    if spa is None:
        failures.append("(c) spa-crawl did not run")
    elif spa.total > 3:
        failures.append(
            f"(c) spa-crawl returned {spa.total} URLs (expected <=3; HTTP-only "
            "cannot see JS routes)"
        )
    elif static is not None and spa.total >= static.total:
        failures.append(
            f"(c) spa-crawl ({spa.total}) not fewer than static ({static.total})"
        )
    else:
        print(
            f"[c] spa-crawl returned {spa.total} URLs (near-zero, < static "
            f"{static.total if static else '?'}) PASS — HTTP-only cannot see JS routes"
        )

    PERF_PATH.parent.mkdir(parents=True, exist_ok=True)
    PERF_PATH.write_text(
        "Sprint H.1 /v2/discover live verification\n"
        f"baseline_chromium={baseline} final_chromium={final}\n\n"
        + "\n".join(results)
        + "\n",
        encoding="utf-8",
    )
    print(f"\nperf written to {PERF_PATH}")

    if failures:
        print("\nFAILURES:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nALL H.1 LIVE CHECKS PASSED (no Chromium spawned).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

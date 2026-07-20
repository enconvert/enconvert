# Sprint F0.5 - Pre-implementation Verification Report (items 1-10)

Date: 2026-06-06
Environment: macOS, Python 3.13.3, venv at `api/gateway/.venv`
Pins: crawl4ai==0.8.9, playwright==1.57.0, playwright-stealth==2.0.0, anthropic==0.105.2
Plan: `V2-VISION-AND-IMPLEMENTATION-PLAN-v3.md` sections 8 (Task F0.5) and 12.
Probes: `scripts/v2_verify/probe_03..09_*.py` (operator-run, no production code changes).
Review: every probe and outcome file passed an adversarial 4-way review
(claim verification with empirical counterfactual, Python code review,
outcome coherence, working-tree purity) before this report was written.

## Summary

| Item | Topic | Status | Outcome file |
|------|-------|--------|--------------|
| 1 | Pin determined | PASS (pre-confirmed; kept newer 0.8.9/0.105.2 deliberately) | plan section 0.1, verify_07_idle_footprint.txt |
| 2 | Model slug `claude-haiku-4-5` | PASS (pre-confirmed 2026-06-04; re-proven live by item 8) | verify_08_litellm_anthropic.txt |
| 3 | Stealth inheritance raw context vs arun() | PASS (with caveat) | verify_03_stealth_inheritance.txt |
| 4 | Hook closure mutation read after arun() | PASS | verify_04_hook_mutation.txt |
| 5 | No double render with pdf=False/screenshot=False | PASS | verify_05_no_double_pdf.txt |
| 6 | page.pdf kwargs pass-through inside hook | PASS (masked-byte identity; see note) | verify_06_pdf_kwargs_pass_through.txt |
| 7 | Runtime RSS with AsyncWebCrawler initialized | FAIL vs 80 MB threshold / PASS vs A4 envelope | verify_07_runtime_footprint.txt |
| 8 | LLMExtractionStrategy via litellm + Anthropic | PASS (end-to-end, keyed) | verify_08_litellm_anthropic.txt |
| 9 | enable_stealth vs V1 playwright_stealth baseline | PARTIAL (no regression observed; fixtures could not discriminate) | verify_09_stealth_baseline.txt |
| 10 | V1 golden PDFs captured | PARTIAL (captures exist; F0.4 test suite does NOT) | verify_10_done.txt |

## Per-item detail

### Item 3 - PASS, mechanism differs from plan's model
`navigator.webdriver` is stealthed on BOTH raw `get_context()/new_page()`
pages and `arun()` pages. Verified cause (empirical counterfactual run with
and without the flag): crawl4ai 0.8.9 passes
`--disable-blink-features=AutomationControlled` UNCONDITIONALLY at browser
launch (its `browser_manager.py` flag assembly; not even gated on
`enable_stealth`), so suppression is browser-process-wide. Caveat: the
per-page playwright_stealth JS overlay still runs ONLY on `arun()` pages;
raw pages lack UA/languages/permissions coherence (observed `prompt` vs
`default` permissions, differing UA strings) and remain PARTIALLY stealthed
until Group F migrates V1 converters to `arun()`. The
`get_page_with_stealth()` shim (plan A2) is NOT required for the webdriver
signal but remains the documented fallback for full per-page coverage.

### Item 4 - PASS
`set_hook("after_goto", ...)` and `set_hook("before_return_html", ...)`
both fire and can stash bytes in a closure dict readable after `arun()`
returns. Firing order: `after_goto` then `before_return_html`;
`before_return_html` receives `html` as kwarg. Gotcha recorded: small
`file://` fixtures trip crawl4ai's near-empty-content detector and the
hook path short-circuits; probe fell back to https://example.com.

### Item 5 - PASS
With `CrawlerRunConfig(pdf=False, screenshot=False)`, crawl4ai issues ZERO
ambient `page.pdf` / `page.screenshot` / CDP `Page.printToPDF` /
`Page.captureScreenshot` calls while a `before_return_html` hook captures
one manual PDF (41,741 bytes). Control run with pdf=True/screenshot=True
proved the instrumentation fires. Note: plan section 12 item 5's suggested
"subscribe to CDP" approach is not implementable (printToPDF is a CDP
command, not an observable event); class-level monkeypatch counting was
used instead and is strictly stronger.

### Item 6 - PASS at masked-byte identity
Full kwargs set (`format`/`width`+`height` mm, `header_template`,
`footer_template`, `display_header_footer`, `tagged`,
`prefer_css_page_size`, `print_background`) passes through `page.pdf()`
inside a hook bit-equivalently to a direct Playwright call: bytes equal
after masking `/CreationDate`, `/ModDate`, trailer `/ID` (level 2), plus
full PyMuPDF structural identity (level 3: page count, MediaBox, text,
content streams). A4 MediaBox, header/footer text, and `/StructTreeRoot`
(tagged) all asserted. Plan's literal "byte-identical" (level 1) is
unachievable across independent Chromium runs (embedded timestamps/IDs) -
masked-byte identity is the correct reading for the A6 byte-identity
strategy. pikepdf is not installed; PyMuPDF (already pinned) was used.

### Item 7 - FAIL vs stale threshold, PASS vs A4 envelope
Two-run averages: import 101.3 MB; instantiated (no `start()`) ~132 MB
(+30.8 MB over import); after `start()`: python ~137 MB + Chromium
children ~545 MB = ~682 MB total; python RSS does not drop post-close.
The 80 MB threshold was calibrated against 0.8.6 and is obsolete at 0.8.9
(import alone exceeds it - known since F0.1). Instantiated RSS fits the
plan A4 gateway envelope (200-280 MB). Mitigations confirmed: lazy
`BrowserManager.get_instance()` (already the F0.2 design) is load-bearing;
do not eager-instantiate at import; `MAX_CONCURRENT_CONTEXTS=1` stands.

### Item 8 - PASS
`anthropic/claude-haiku-4-5` resolves in the vendored litellm registry
(unclecode-litellm 1.81.13), the deprecated `provider=` constructor kwarg
is correctly blocked in favour of `llm_config=LLMConfig(...)`, and a live
keyed extraction over a 1 KB HTML table returned structured blocks in
1.44 s (prompt=787, completion=133 tokens). No 422, no model-resolution
error.

### Item 9 - PARTIAL, no regression observed
Primary fixture (scrapingcourse.com/cloudflare-challenge): NEITHER arm
cleared the challenge - too strong to discriminate stealth layers with
magic/simulate_user deliberately disabled. Secondary (nowsecure.nl): both
arms identical (HTTP 200, body 179,917 bytes each). The FAIL condition
(V1 baseline clearing where enable_stealth does not) was never observed
on any fixture. Production configs additionally enable
magic/simulate_user/override_navigator (plan A1), which this probe
intentionally excluded.

### Item 10 - PARTIAL
Golden 5-URL captures with manifest exist (f03_sanity/baseline, captured
2026-06-05). Task F0.4's regression / byte-identity / memory TEST SUITE
has no commit on any branch and no test files - it must not be assumed
present at the Phase 0.5 exit gate. See verify_10_done.txt.

## Decisions

1. **litellm bridge selected** for Group F.6 LLM extraction. The direct
   `anthropic.AsyncAnthropic` fallback in `schema_llm.py` is NOT needed
   now; keep it as the documented degradation path only.
2. **`playwright_stealth==2.0.0` stays pinned - mandatory, not optional.**
   crawl4ai's `enable_stealth` is implemented by `StealthAdapter`, which
   imports playwright_stealth and silently no-ops if the import fails.
   Removing the pin would silently disable V2 stealth. This closes the
   plan section 15 open question ("keep playwright_stealth long-term?"):
   keep, permanently, as a load-bearing transitive engine.
3. **No `get_page_with_stealth()` shim now.** The decisive webdriver
   signal is inherited globally via an unconditional launch flag. Full
   per-page stealth coverage arrives with the Group F `arun()` migration
   as planned; the shim remains the documented fallback if an anti-bot
   regression appears on raw-context converters before then.
4. **Memory plan**: treat the 80 MB threshold as superseded; budget
   ~132 MB python-side idle (instantiated) and ~680 MB python+Chromium
   operational, inside A4's worst-case math. First sustained swap event
   still triggers the 2 GB upgrade per A4.
5. **PDF byte-identity criterion**: adopt masked-byte identity (timestamps
   and document IDs masked) as the formal level for A6/F.1 regression
   tests; literal byte equality is unachievable across Chromium runs.

## crawl4ai 0.8.9 facts Group F must respect (discovered here)

- `crawler_strategy.browser` does NOT exist; the Playwright Browser is at
  `crawler.crawler_strategy.browser_manager.browser` (plan A2/section 12
  item 3 wording is stale; BrowserManager already handles this).
- `LLMExtractionStrategy(provider=...)` is deprecated/blocked; use
  `llm_config=LLMConfig(provider=..., api_token=...)`.
- `file://` URLs take a no-browser fast path that SILENTLY SKIPS ALL
  HOOKS unless `process_in_browser=True` (or pdf/screenshot/js_code force
  the browser path).
- Very small documents can trip the near-empty-content anti-bot heuristic
  and short-circuit before `after_goto`.
- `--disable-blink-features=AutomationControlled` is always injected by
  crawl4ai at launch, independent of `enable_stealth`.

## Follow-ups (not done in this sprint - F0.5 forbids production edits)

1. Land Task F0.4 (regression / byte-identity / memory tests). Item 10 is
   PARTIAL until then.
2. One-line docstring correction in
   `services/browser/converters/browser_manager.py` (lines ~45-50): raw
   pages are "partially stealthed (launch-flag webdriver suppression, no
   per-page JS overlay)", not "un-stealthed". Fold into the first Group F
   commit.
3. Optional: re-discriminate item 9 with production-config arms
   (magic/simulate_user on) or a lighter anti-bot fixture if blocked-page
   rates regress after the Group F migration.

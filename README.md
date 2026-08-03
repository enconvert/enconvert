<h1 align="center">EnConvert</h1>

<p align="center">
  HTTP API for converting files and reading web pages.<br/>
  Accepts a URL or a file and returns Markdown, JSON, HTML, a screenshot, or a PDF. Supports 46 file formats.
</p>

<p align="center">
  <a href="https://github.com/enconvert/enconvert/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue.svg" alt="License: AGPL-3.0"></a>
  <a href="https://github.com/enconvert/enconvert/releases"><img src="https://img.shields.io/github/v/release/enconvert/enconvert?include_prereleases" alt="Release"></a>
  <a href="https://github.com/enconvert/enconvert/stargazers"><img src="https://img.shields.io/github/stars/enconvert/enconvert?style=social" alt="GitHub stars"></a>
</p>

---

EnConvert reads two kinds of input through one API: files, which it converts between formats, and web pages, which it renders and returns as text or images.

This repository is the API under AGPL-3.0: request routing, the conversion pipelines, storage, background jobs, and a self-hostable render path. The hosted service at [enconvert.com](https://enconvert.com) runs this code together with additional closed components — the multi-engine render ladder, anti-bot rendering, render-quality scoring, LLM-based extraction, and semantic diffing. The table under [Open source vs Cloud](#open-source-vs-cloud) lists exactly which parts are in this repository and which are not.

## Endpoints

| Verb | | What it does |
|---|---|---|
| **perceive** | `URL → MD/JSON/PNG/PDF` | Renders a URL once and returns Markdown, HTML, a screenshot, a PDF, extracted links, and structured data. |
| **convert** | `FILE → FILE` | Converts between 46 file types, including PDF, DOCX, XLSX, PPTX, HEIC, SVG, and CSV. |
| **discover** | `SITE → URL MAP` | Lists a site's URLs from its sitemap and an HTTP crawl, without rendering pages. |
| **lookup** | `QUERY → RESULTS` | Queries web, news, scholar, and maps search. |
| **distill** | `SCHEMA → JSON` | Extracts structured data from a page against a schema you supply. |
| **ingest** | `SITE → CHUNKS` | Crawls a site and returns JSONL chunks suitable for RAG indexing. |
| **watch** | `PAGE → DIFF` | Checks a page on a schedule and sends a webhook when it changes. |

## Quick start (hosted)

Create an API key at [enconvert.com](https://enconvert.com), then:

```bash
# perceive: a live page → Markdown
curl -X POST https://api.enconvert.com/v2/perceive \
  -H "X-API-Key: YOUR_API_KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "outputs": ["markdown"]}'
```

```bash
# convert: a URL → PDF
curl -X POST https://api.enconvert.com/v1/convert/url-to-pdf \
  -H "X-API-Key: YOUR_API_KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
```

## Self-hosting

```bash
git clone https://github.com/enconvert/enconvert.git
cd enconvert
docker compose up
```

The self-hosted build performs the file conversions and renders pages with a plain headless browser. See **[SELF_HOST.md](./SELF_HOST.md)** for the full capability list and configuration.

## Render quality scoring

A page fetch can succeed at the HTTP level and still return something other than the page — an anti-bot challenge, a login wall, a cookie interstitial, or an unhydrated SPA shell. Downstream consumers usually cannot tell the difference from the response body alone.

The hosted service scores every read as `render_quality` (0.0–1.0) and flags a blocked or login-walled result rather than returning it as ordinary content. This scoring is not part of the open build; a self-hosted render returns whatever the browser produced, unscored.

## Open source vs Cloud

| | Self-hosted (this repo) | Cloud ([enconvert.com](https://enconvert.com)) |
|---|:---:|:---:|
| 46-format file conversion | ✅ | ✅ |
| perceive: URL → Markdown / screenshot / PDF | basic render | **Chromium engine, scored** |
| discover: site → URL map | ✅ | ✅ |
| lookup: live web search | results only | **+ render, extract & cited answers** |
| ingest: site → RAG chunks | ✅ | ✅ |
| watch: change monitoring | content-hash | **semantic diff** |
| Anti-bot / stealth rendering | — | ✅ |
| `render_quality` read scoring | — | ✅ |
| distill: schema → structured JSON | CSS pass | **+ LLM extraction** |
| Managed dashboard, teams, billing | — | ✅ |

Rows marked — are absent from this repository. The self-hosted build is functional without them: it converts files, maps sites, crawls to chunks, and renders pages with a plain headless browser.

## SDKs

Official SDKs are MIT-licensed and default to the cloud API:

- [Node / TypeScript SDK](https://github.com/enconvert/node-sdk)
- [Model Context Protocol server](https://github.com/enconvert/mcp) — one-command install for Claude, Cursor, and Windsurf

## License

EnConvert is open source under **AGPL-3.0** — see [LICENSE](./LICENSE). The SDKs and MCP server are **MIT**. The hosted service at [enconvert.com](https://enconvert.com) includes the additional components listed above.

---

<p align="center">
  <sub>Pst — if EnConvert is useful to you, consider leaving a ⭐. It genuinely helps.</sub>
</p>

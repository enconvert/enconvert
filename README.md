<h1 align="center">EnConvert</h1>

<p align="center">
  <b>The web lies to your agent. EnConvert doesn't.</b><br/>
  One API reads any file (46 formats) and any page into clean Markdown, JSON, screenshots, or PDF — and tells you when a read was blocked.
</p>

<p align="center">
  <a href="https://github.com/enconvert/enconvert/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue.svg" alt="License: AGPL-3.0"></a>
  <a href="https://github.com/enconvert/enconvert/releases"><img src="https://img.shields.io/github/v/release/enconvert/enconvert?include_prereleases" alt="Release"></a>
  <a href="https://github.com/enconvert/enconvert/stargazers"><img src="https://img.shields.io/github/stars/enconvert/enconvert?style=social" alt="GitHub stars"></a>
</p>

---

An LLM is brilliant and blind. Hand it a URL and it will confidently summarize a Cloudflare challenge, a cookie wall, or an empty SPA shell — and never tell you. Scrapers stop at pages. Converters stop at files. Your agent needs both, and it shouldn't take two vendors to get them.

EnConvert is the conversion and reading layer for agents: point it at a URL or a file, get back clean Markdown, JSON, a screenshot, or a PDF. This repository is the **open-source API** — the orchestration, routing, storage, jobs, and self-hostable engine — under the AGPL-3.0. The managed cloud at [enconvert.com](https://enconvert.com) runs this same code plus the advanced rendering engine and the quality scoring that make reads trustworthy at scale.

## Six verbs, one API

| Verb | | What it does |
|---|---|---|
| **perceive** | `URL → MD/JSON/PNG/PDF` | Render any URL once and return Markdown, HTML, a screenshot, a PDF, links, and structured data. |
| **convert** | `FILE → FILE` | Open 46 file types in one request — PDF, DOCX, XLSX, PPTX, HEIC, SVG, CSV, and more, in and out. |
| **discover** | `SITE → URL MAP` | Map every URL on a site from its sitemap and an HTTP crawl, without rendering a page. |
| **lookup** | `QUERY → RESULTS` | Search the live web (web, news, scholar, maps) from inside the conversation. |
| **distill** | `SCHEMA → JSON` | Pass a schema, get structured data back from any page. |
| **ingest** | `SITE → CHUNKS` | Crawl a whole site into RAG-ready JSONL chunks in one call. |
| **watch** | `PAGE → DIFF` | Get a webhook the moment a page you track changes. |

## Quick start (hosted)

Grab an API key at [enconvert.com](https://enconvert.com), then:

```bash
# perceive: a live page → clean Markdown for your LLM pipeline
curl -X POST https://api.enconvert.com/v2/perceive \
  -H "X-API-Key: YOUR_API_KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "outputs": ["markdown"]}'
```

```bash
# convert: any file → PDF
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

The self-hosted build handles the commodity conversions out of the box and renders pages with a plain headless browser. See **[SELF_HOST.md](./SELF_HOST.md)** for the full capability list and configuration.

## Every read comes with a receipt

The hardest part of reading the web isn't fetching a page — it's knowing whether what came back is real. The **EnConvert cloud** scores every read (`render_quality`, 0.0–1.0): a blocked or login-walled page comes back *flagged*, not passed off as content. That plus real anti-bot rendering, cross-result answer synthesis, and semantic change diffing is what the cloud adds on top of this open core.

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

The eyes are real today. The advanced engine, the scoring, and the semantic layer live in the cloud — this repo is the honest open core they run on.

## SDKs

Official SDKs are MIT-licensed and default to the cloud API:

- [Node / TypeScript SDK](https://github.com/enconvert/node-sdk)
- [Model Context Protocol server](https://github.com/enconvert/mcp) — one-command install for Claude, Cursor, and Windsurf

## License

EnConvert is open source under **AGPL-3.0** — see [LICENSE](./LICENSE). The SDKs and MCP server are **MIT**. The managed cloud at [enconvert.com](https://enconvert.com) includes additional features.

---

<p align="center">
  <sub>Pst — if EnConvert is useful to you, consider leaving a ⭐. It genuinely helps.</sub>
</p>

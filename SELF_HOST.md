# Self-hosting EnConvert

EnConvert is fully self-hostable. You run the exact same orchestration layer that
powers the cloud — the API, job workers, storage, discovery, ingest, and watch
subsystems — with zero reliance on our servers.

What you **don't** get by self-hosting is the part that's genuinely hard: the
advanced rendering engine, the read-quality scoring, and the semantic layer. The
self-hosted build reads pages with a **plain headless Chromium via Playwright** —
no anti-bot handling, no stealth, no escalation, and no `render_quality` receipt.
For ordinary pages that's fine. For the pages that actually fight back — and for
knowing when a read was blocked — that's what the [cloud](https://enconvert.com) is for.

## Quick start (Docker)

```bash
git clone https://github.com/enconvert/enconvert.git
cd enconvert
docker compose up --build
```

This brings up:

- `gateway` — the EnConvert API (port 8010)
- `postgres` — job / usage / watch state
- `minio` — S3-compatible object storage for conversion outputs
- `unoserver` — LibreOffice document → PDF conversion

The API is then available at `http://localhost:8010`. Health check: `GET /health`.

## Why self-host?

Self-hosting EnConvert is useful when data must stay inside your own
infrastructure — strict security or compliance environments, air-gapped networks,
or wanting full control over the stack under the AGPL-3.0. It's the right choice
for commodity conversion and ordinary-page reads you want to run yourself.

## Configuration

Configuration is entirely environment-driven — there is no config file to edit.
`docker-compose.yml` wires sensible local defaults; override these for a real
deployment:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `JWT_SECRET` | Secret for signing API/session tokens |
| `DO_SPACES_KEY` / `DO_SPACES_SECRET` | S3-compatible storage credentials (MinIO in the compose file) |
| `DO_SPACES_ENDPOINT` / `DO_SPACES_BUCKET` | Storage endpoint + bucket for outputs |
| `UNOSERVER_URL` | Address of the unoserver instance (office → PDF) |
| `SERPER_API_KEY` | Your own search-provider key, to enable `lookup` |

## What works self-hosted

Out of the box, with no external services:

- **convert** — 46-format file conversion (PDF, DOCX, XLSX, PPTX, images, CSV, …)
- **perceive** — URL → Markdown / HTML / screenshot / PDF via plain headless Chromium
- **discover** — site → URL map (sitemap + HTTP crawl)
- **lookup** — live web search results (bring your own `SERPER_API_KEY`)
- **ingest** — crawl a site into RAG-ready JSONL chunks
- **watch** — change monitoring with content-hash diffing
- **Async jobs, batch conversions, webhooks, email notifications**

## Considerations — where the cloud earns its keep

These capabilities are powered by the EnConvert cloud engine and are **not part of
the self-hosted build**. Each is a concrete reason to reach for
[enconvert.com](https://enconvert.com), which runs this same codebase plus the engine:

- **Trustworthy reads (`render_quality`).** Every cloud read comes with a receipt —
  a 0.0–1.0 score of how faithfully the page rendered. A blocked or login-walled
  page comes back *flagged*, not silently passed off as content. The self-hosted
  scorer only flags a near-empty body; it never detects an anti-bot challenge or a
  login wall. Nobody else scores reads this way.
- **Real anti-bot rendering.** The cloud drives a hardened Chromium with stealth,
  TLS-fingerprint impersonation, rotating/residential proxies, and an escalation
  ladder for IP blocks and bot detection. Self-hosted `perceive` is a plain
  headless browser — great for ordinary pages, no match for pages that fight back.
- **`perceive` at fidelity + scale.** Mobile emulation, geolocation, browser
  actions, a dedicated rendering pool, and large batches (up to 400 files) are
  cloud-only.
- **`lookup` that does more than search.** Self-hosted `lookup` returns the **search
  results only**. The cloud renders the top results, runs structured extraction
  across them, and **synthesizes one cited, grounded answer** to your query — the
  semantic layer that makes lookup beat "search + scrape." That understanding is
  cloud-only.
- **`distill` with LLM extraction.** Self-hosted distill runs the free CSS pass
  only. The cloud adds schema synthesis and a metered LLM extraction fallback for
  pages a CSS selector can't handle.
- **`watch` with semantic diffing.** Self-hosted watch detects *that* a page
  changed (content-hash). The cloud tells you *what* meaningfully changed —
  structured/semantic diffing across lists, tables, and metadata.
- **Managed platform.** Dashboard, teams, usage analytics, billing, opt-in overage,
  domain-restricted keys, and a public status page.

Every URL EnConvert touches — self-hosted or cloud — is SSRF-screened: no private
IPs, no metadata endpoints, no internal networks.

For any of the above, use the hosted API at [enconvert.com](https://enconvert.com).

## Running behind a reverse proxy

An example Nginx configuration is provided in [`deploy/nginx.example.conf`](./deploy/nginx.example.conf).

## Contributing

Self-hosting and contributing go hand in hand — fork the repo, make your changes,
run the tests, and open a PR. See the issues tab for good first tasks.

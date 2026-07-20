# Self-hosting Enconvert

Enconvert is fully self-hostable. You run the exact same orchestration layer that
powers the cloud — the API, job workers, storage, discovery, batch, and watch
subsystems — with zero reliance on our servers.

## Why self-host?

Self-hosting Enconvert is particularly useful when data must stay inside your own
infrastructure — strict security or compliance environments, air-gapped networks,
or simply wanting full control over the stack. You also get to customize and
extend the service however you like under the AGPL-3.0.

## Quick start (Docker)

```bash
git clone https://github.com/enconvert/enconvert.git
cd enconvert
docker compose up --build
```

This brings up:

- `gateway` — the Enconvert API (port 8010)
- `postgres` — job/usage/watch state
- `minio` — S3-compatible object storage for conversion outputs
- `unoserver` — LibreOffice document → PDF conversion

The API is then available at `http://localhost:8010`. Health check: `GET /health`.

## Configuration

Configuration is entirely environment-driven — there is no config file to edit.
The `docker-compose.yml` wires sensible local defaults; override these for a real
deployment. The variables that matter:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `JWT_SECRET` | Secret for signing API/session tokens |
| `DO_SPACES_KEY` / `DO_SPACES_SECRET` | S3-compatible storage credentials (MinIO in the compose file) |
| `DO_SPACES_ENDPOINT` / `DO_SPACES_BUCKET` | Storage endpoint + bucket for outputs |
| `UNOSERVER_URL` | Address of the unoserver instance (office → PDF) |
| `ANTHROPIC_API_KEY` | *(optional)* your own key if you enable LLM extraction |

Set them in your environment or in the `environment:` block of `docker-compose.yml`.

## What works self-hosted

Out of the box, with no external services:

- **URL / HTML / document → PDF**
- **Full-page screenshots**
- **Anything → Markdown** (HTML, PDF, office docs, epub, …)
- **Image conversions** (PNG/JPEG/WebP/HEIC/SVG/…)
- **Lightweight conversions** (CSV/JSON/YAML/TOML/Markdown/HTML)
- **URL discovery** — sitemap parsing + link crawling
- **Change watching** — content-hash diffing to detect when a page changes
- **Async jobs, batch conversions, webhooks, email notifications**

Rendering uses a plain headless Chromium. Office → PDF uses LibreOffice via
`unoserver`.

## Considerations

Some advanced capabilities are powered by the Enconvert cloud engine and are
**not part of the self-hosted build**. Requests that require them return
`HTTP 501` naming the missing capability:

- **Advanced anti-bot / stealth rendering** — rotating and stealth proxies, TLS
  fingerprint impersonation, and the escalation ladder for handling IP blocks and
  robot-detection mechanisms. The self-hosted browser handles ordinary sites;
  heavily protected sites may need the cloud engine.
- **Browser actions** — click / scroll / type / execute-JavaScript flows.
- **Mobile emulation and geolocation-specific rendering.**
- **LLM structured extraction** — schema synthesis and question-answering over
  pages. You can enable a basic path by supplying your own `ANTHROPIC_API_KEY`,
  but the tuned extraction pipeline is cloud-only.
- **Semantic change diffing** — self-hosted watching uses content-hash diffing;
  structured/semantic diffs are cloud-only.
- **Managed dashboard, teams, and billing.**

For these, use the hosted API at [enconvert.com](https://enconvert.com), which
runs this same codebase plus the cloud engine.

## Running behind a reverse proxy

An example Nginx configuration is provided in [`deploy/nginx.example.conf`](./deploy/nginx.example.conf).

## Contributing

Self-hosting and contributing go hand in hand — fork the repo, make your changes,
run the tests, and open a PR. See the issues tab for good first tasks.

<h1 align="center">Enconvert</h1>

<p align="center">
  <b>Open-source API to convert anything on the web into clean, AI-ready data.</b><br/>
  URLs and documents → PDF, screenshots, Markdown, and structured JSON — through one API.
</p>

<p align="center">
  <a href="https://github.com/enconvert/enconvert/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue.svg" alt="License: AGPL-3.0"></a>
  <a href="https://github.com/enconvert/enconvert/stargazers"><img src="https://img.shields.io/github/stars/enconvert/enconvert?style=social" alt="GitHub stars"></a>
</p>

---

Enconvert is a conversion engine built for people who need reliable, structured output from messy inputs. Point it at a URL or hand it a file, and get back a pixel-accurate PDF, a full-page screenshot, clean Markdown, or JSON matching a schema you define.

It is **open source and available as a hosted service**. Run it yourself under the AGPL-3.0, or skip the ops and use the managed API at [enconvert.com](https://enconvert.com).

## Why Enconvert?

- **One API, every format.** URL → PDF, HTML → PDF, document → PDF, page → screenshot, anything → Markdown, page → structured JSON.
- **Built for automation.** Async jobs, batch conversions, webhooks, and change-watching for pages you care about.
- **Developer-first.** Clean REST endpoints, typed SDKs, signed download URLs, sensible defaults.
- **Open source.** Developed in the open under the AGPL-3.0. Self-host the whole orchestration layer with zero reliance on our servers.

## Quick start (hosted)

Grab an API key at [enconvert.com](https://enconvert.com), then:

```bash
curl -X POST https://api.enconvert.com/v1/convert/url-to-pdf \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
```

Turn a live page into clean Markdown for your LLM pipeline:

```bash
curl -X POST https://api.enconvert.com/v2/perceive \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "outputs": ["markdown"]}'
```

## Self-hosting

Enconvert is self-hostable with Docker in a couple of commands:

```bash
git clone https://github.com/enconvert/enconvert.git
cd enconvert
docker compose up
```

See **[SELF_HOST.md](./SELF_HOST.md)** for configuration and the full capability list.

The self-hosted build does basic rendering with a plain headless browser and handles all the commodity conversions out of the box. The **cloud version at [enconvert.com](https://enconvert.com) adds advanced rendering** — stealth/anti-bot page loading for protected sites, browser actions, geolocation and mobile emulation, LLM-powered structured extraction, and a managed dashboard with teams and billing.

## Open source vs Cloud

| | Self-hosted (this repo) | Cloud ([enconvert.com](https://enconvert.com)) |
|---|:---:|:---:|
| URL / HTML / doc → PDF | ✅ | ✅ |
| Full-page screenshots | ✅ | ✅ |
| Anything → Markdown | ✅ | ✅ |
| Image & lightweight conversions | ✅ | ✅ |
| URL discovery (sitemap + crawl) | ✅ | ✅ |
| Change watching (content-hash) | ✅ | ✅ |
| Async jobs, batch, webhooks | ✅ | ✅ |
| Advanced anti-bot / stealth rendering | — | ✅ |
| Browser actions, mobile, geolocation | — | ✅ |
| LLM structured extraction | — | ✅ |
| Semantic change diffing | — | ✅ |
| Managed dashboard, teams, billing | — | ✅ |

## SDKs

Official SDKs are MIT-licensed and default to the cloud API:

- [Node / TypeScript](https://github.com/enconvert/node-sdk)
- [Model Context Protocol server](https://github.com/enconvert/mcp)

## License

Enconvert is open source under the **AGPL-3.0** license — see [LICENSE](./LICENSE). The SDKs and client libraries are licensed under the **MIT License**. The managed cloud service at [enconvert.com](https://enconvert.com) includes additional features.

---

<p align="center">
  <sub>Pst — if Enconvert is useful to you, consider leaving a ⭐. It genuinely helps.</sub>
</p>

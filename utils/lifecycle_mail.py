"""Lifecycle (onboarding) email egress — separate from utils/email_notifier.py.

These are personal founder emails, not transactional notifications, so they
have their own sender identity (config.LIFECYCLE_FROM_EMAIL, Reply-To on the
main domain), their own minimal letter-like shell (no color bar, no logo, no
buttons), List-Unsubscribe / List-Unsubscribe-Post headers (RFC 8058 one-click
— on lifecycle mail ONLY, never on transactional/auth mail), and a visible
unsubscribe link in the footer.

Copy rules enforced by tests/v2/test_lifecycle_mail.py:
    * no email prints a quota number
    * no email says "forever" or "limited time" about any plan
    * snippets authenticate with the X-API-Key header, never a Bearer token
    * every interpolated value is html.escape()d
    * the stuck email truncates error_message to 400 chars AFTER escaping
    * founder_call has no upgrade CTA and no plan table

Every sender takes (recipient dict, context dict, now) and returns bool,
never raising — the caller (services/lifecycle_emails.py) records False as a
retryable failed send. Suppression/opt-out checks live in the candidate
layer, never here.
"""
from __future__ import annotations

import html
import logging
import os
from datetime import datetime
from typing import Dict

import requests

import config
from utils import unsub_token
from utils.email_notifier import _plain_text

logger = logging.getLogger(__name__)

BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")

_DOCS_URL = "https://www.enconvert.com/docs/introduction"

# Reference snippets. The placeholder key is deliberately the documented
# sk_live_ prefix; auth is ALWAYS the X-API-Key header (never Bearer).
_CURL_LINE = (
    "curl -X POST https://api.enconvert.com/v2/perceive \\\n"
    '  -H "X-API-Key: sk_live_YOUR_KEY" \\\n'
    '  -H "Content-Type: application/json" \\\n'
    '  -d \'{"url": "https://example.com", "format": "markdown"}\''
)
_MCP_LINE = "npx @enconvert/mcp setup --api-key sk_live_YOUR_KEY"


def _frontend_base() -> str:
    """Frontend base for links in email bodies — same convention as
    utils/email_notifier._build_watcher_change_html (DASHBOARD_ORIGIN with a
    scheme guard so a misconfigured value cannot smuggle a bad href)."""
    origin = os.getenv("DASHBOARD_ORIGIN", "https://enconvert.com").rstrip("/")
    if not origin.startswith(("https://", "http://")):
        origin = "https://enconvert.com"
    return origin


def _lifecycle_shell(body_html: str, unsub_url: str) -> str:
    """Minimal letter-like shell: plain paragraphs, a signature, and a small
    grey footer whose unsubscribe link is always visible. ``body_html`` must
    already be escaped by the caller; everything added here is static except
    the (attribute-escaped) unsubscribe URL."""
    safe_unsub = html.escape(unsub_url, quote=True)
    return f"""<html><body style="margin:0;padding:0;background:#ffffff;">
  <div style="max-width:560px;margin:0 auto;padding:24px 20px;
              font-family:Georgia,'Times New Roman',serif;
              font-size:16px;line-height:1.65;color:#222;">
    {body_html}
    <p style="margin-top:28px;">— Het<br>
       <span style="color:#666;font-size:14px;">Founder, EnConvert</span></p>
    <div style="margin-top:32px;padding-top:14px;border-top:1px solid #e5e5e5;
                font-size:12px;color:#888;">
      <p style="margin:0;">You're getting a handful of these while you set up
      EnConvert. Don't want them?
      <a href="{safe_unsub}" style="color:#888;">Unsubscribe</a>
      and I'll stop.</p>
    </div>
  </div>
</body></html>"""


def _send_lifecycle(
    to_email: str, to_name: str, subject: str, html_body: str, user_id: int
) -> bool:
    """POST one lifecycle email to Brevo. Returns False (never raises) on any
    problem, including an unset unsubscribe secret — a lifecycle email with a
    dead unsubscribe link must never leave the building."""
    if not BREVO_API_KEY:
        logger.error("BREVO_API_KEY not set")
        return False
    try:
        token = unsub_token.make_token(user_id)  # raises when secret unset
        unsub_url = f"{config.LIFECYCLE_UNSUB_URL}?t={token}"
        full_html = _lifecycle_shell(html_body, unsub_url)
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "content-type": "application/json"},
            json={
                "sender": {
                    "name": config.LIFECYCLE_FROM_NAME,
                    "email": config.LIFECYCLE_FROM_EMAIL,
                },
                "replyTo": {
                    "email": config.LIFECYCLE_REPLY_TO,
                    "name": config.LIFECYCLE_FROM_NAME,
                },
                "to": [{"email": to_email, "name": to_name or to_email}],
                "subject": subject,
                "htmlContent": full_html,
                "textContent": _plain_text(full_html),
                "tags": ["lifecycle"],
                "headers": {
                    "List-Unsubscribe": (
                        f"<mailto:{config.LIFECYCLE_REPLY_TO}?subject=unsubscribe>, "
                        f"<{unsub_url}>"
                    ),
                    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                },
            },
            timeout=(3.05, 10),
        )
        response.raise_for_status()
        # Log the user id, never the address (email is PII).
        logger.info("lifecycle email sent (user %s)", user_id)
        return True
    except Exception as exc:  # noqa: BLE001 — senders must never raise
        logger.error("lifecycle email send failed (user %s): %s", user_id, exc)
        return False


def _first_name(recipient: Dict) -> str:
    name = (recipient.get("name") or "").strip()
    return html.escape(name.split()[0]) if name else "there"


def _pre(snippet: str) -> str:
    """Monospace block for a snippet (escaped; the shell stays button-free)."""
    return (
        '<pre style="background:#f6f6f6;padding:12px;font-size:13px;'
        "font-family:Menlo,Consolas,monospace;overflow-x:auto;"
        f'white-space:pre-wrap;">{html.escape(snippet)}</pre>'
    )


# ─── The eight senders ───────────────────────────────────────────────────────


def welcome_verify(recipient: Dict, context: Dict, now: datetime) -> bool:
    """Canonical welcome + verify copy. The backend sends its own merged
    welcome/verify at signup (sections/api/views.py); this sender exists so
    the lifecycle system owns one canonical version of the copy for parity
    and for any future re-send path."""
    verify_url = html.escape(str(context.get("verify_url", "")), quote=True)
    body = f"""
      <p>Hi {_first_name(recipient)},</p>
      <p>I'm Het — I built EnConvert. Thanks for signing up.</p>
      <p>One thing before anything else works: please verify your email.
      The link is good for a day.</p>
      <p><a href="{verify_url}" style="color:#1e4a7a;">Verify my email</a></p>
      <p>Once you're in, grab an API key from the dashboard and you can start
      converting. If anything is confusing, just reply to this email — it
      comes straight to me.</p>
    """
    return _send_lifecycle(
        recipient["email"], recipient.get("name", ""),
        "Welcome to EnConvert — one click to get started",
        body, recipient["user_id"],
    )


def unverified_nudge_1(recipient: Dict, context: Dict, now: datetime) -> bool:
    verify_url = html.escape(str(context.get("verify_url", "")), quote=True)
    body = f"""
      <p>Hi {_first_name(recipient)},</p>
      <p>You created an EnConvert account a little while ago but the verify
      link hasn't been clicked yet, so nothing works for you right now.</p>
      <p>Here's a fresh link:
      <a href="{verify_url}" style="color:#1e4a7a;">verify my email</a>.</p>
      <p>If the first email went to spam, that would explain it — this one
      hopefully made it through. And if you didn't sign up, ignore this and
      nothing happens.</p>
    """
    return _send_lifecycle(
        recipient["email"], recipient.get("name", ""),
        "Your EnConvert verify link (fresh one inside)",
        body, recipient["user_id"],
    )


def unverified_nudge_2(recipient: Dict, context: Dict, now: datetime) -> bool:
    verify_url = html.escape(str(context.get("verify_url", "")), quote=True)
    body = f"""
      <p>Hi {_first_name(recipient)},</p>
      <p>Your EnConvert account has been sitting unverified since yesterday.
      This is the last nudge I'll send about it.</p>
      <p>Fresh link: <a href="{verify_url}" style="color:#1e4a7a;">verify my
      email</a>.</p>
      <p>If the link doesn't work, or something on our end is broken, reply
      and tell me what you're seeing — I read these myself.</p>
    """
    return _send_lifecycle(
        recipient["email"], recipient.get("name", ""),
        "Last nudge — your EnConvert account isn't verified",
        body, recipient["user_id"],
    )


def verified_no_key(recipient: Dict, context: Dict, now: datetime) -> bool:
    keys_url = html.escape(f"{_frontend_base()}/dashboard/api-keys", quote=True)
    docs_url = html.escape(_DOCS_URL, quote=True)
    body = f"""
      <p>Hi {_first_name(recipient)},</p>
      <p>You verified your email yesterday but haven't created an API key
      yet — and without one, EnConvert can't do anything for you.</p>
      <p>It's one click: <a href="{keys_url}" style="color:#1e4a7a;">create
      an API key</a>. Then a first request looks like this:</p>
      {_pre(_CURL_LINE)}
      <p>The docs walk through everything else:
      <a href="{docs_url}" style="color:#1e4a7a;">enconvert.com/docs</a>.</p>
      <p>Stuck, or not sure EnConvert does what you need? Reply and ask — I
      answer these personally.</p>
    """
    return _send_lifecycle(
        recipient["email"], recipient.get("name", ""),
        "Your EnConvert account is ready — it just needs a key",
        body, recipient["user_id"],
    )


def key_never_used(recipient: Dict, context: Dict, now: datetime) -> bool:
    docs_url = html.escape(_DOCS_URL, quote=True)
    body = f"""
      <p>Hi {_first_name(recipient)},</p>
      <p>You created an API key yesterday but it hasn't made a request yet.
      Usually that means the first call didn't come together — so here are
      the two fastest ways in.</p>
      <p>From a terminal (the key goes in the <code>X-API-Key</code>
      header):</p>
      {_pre(_CURL_LINE)}
      <p>Or, if you work with an AI agent, wire up our MCP server and let it
      drive EnConvert for you:</p>
      {_pre(_MCP_LINE)}
      <p>Full reference: <a href="{docs_url}"
      style="color:#1e4a7a;">enconvert.com/docs</a>. If neither of these is
      what you were trying to do, reply and tell me what is.</p>
    """
    return _send_lifecycle(
        recipient["email"], recipient.get("name", ""),
        "Your API key hasn't made its first request",
        body, recipient["user_id"],
    )


def stuck(recipient: Dict, context: Dict, now: datetime) -> bool:
    """Context: error_type (str), failed_count (int), error_message (str,
    truncated to 400 chars AFTER escaping — it can contain page-derived
    text)."""
    error_type = html.escape(str(context.get("error_type") or "an error"))
    error_message = html.escape(str(context.get("error_message") or ""))[:400]
    failed_count = int(context.get("failed_count") or 0)
    if error_message:
        detail = (
            '<p style="color:#666;font-size:14px;">Last error: '
            f"<code>{error_type}</code>: {error_message}</p>"
        )
    else:
        detail = (
            '<p style="color:#666;font-size:14px;">Last error: '
            f"<code>{error_type}</code></p>"
        )
    body = f"""
      <p>Hi {_first_name(recipient)},</p>
      <p>I can see {failed_count} failed requests on your account and no
      successful one yet. That's on us until proven otherwise, so let me
      help.</p>
      {detail}
      <p>The most common cause is authentication: the key goes in the
      <code>X-API-Key</code> header, not anywhere else. A known-good request
      looks like this:</p>
      {_pre(_CURL_LINE)}
      <p>If that's not it, reply with what you're trying to convert and the
      request you sent, and I'll figure out what's wrong — usually same
      day.</p>
    """
    return _send_lifecycle(
        recipient["email"], recipient.get("name", ""),
        "Your EnConvert requests are failing — let me help",
        body, recipient["user_id"],
    )


# Detected first-use surface -> the adjacent thing worth trying next.
_SURFACE_NEXT: Dict[str, str] = {
    "mcp": "your agent is wired up — next, try <code>ingest</code>: point it "
           "at a whole docs site and it will crawl and convert every page",
    "api": "next, try <code>perceive</code> with "
           "<code>format=markdown</code> — clean, main-content markdown from "
           "any URL",
    "sdk": "next, try <code>perceive</code> with "
           "<code>format=markdown</code> — clean, main-content markdown from "
           "any URL",
    "cli": "next, try <code>discover</code> to map a site's URLs before you "
           "crawl it",
    "extension": "next, try a full-page PDF or screenshot of a page straight "
                 "from your browser",
    "n8n": "next, drop a watch node in a workflow — you'll get pinged when a "
           "page you care about changes",
    "web": "next, try the API directly — <code>perceive</code> with "
           "<code>format=markdown</code> turns any URL into clean markdown",
}

_SURFACE_LABEL: Dict[str, str] = {
    "mcp": "over MCP",
    "api": "through the API",
    "sdk": "through one of our SDKs",
    "cli": "from the CLI",
    "extension": "from the browser extension",
    "n8n": "from n8n",
    "web": "from the dashboard",
}


def activated(recipient: Dict, context: Dict, now: datetime) -> bool:
    """Context: surface (web|api|sdk|mcp|extension|cli|n8n)."""
    surface = str(context.get("surface") or "api")
    label = _SURFACE_LABEL.get(surface, _SURFACE_LABEL["api"])
    suggestion = _SURFACE_NEXT.get(surface, _SURFACE_NEXT["api"])
    call_line = ""
    call_url = getattr(config, "FOUNDER_CALL_URL", None)
    if call_url:
        safe_call = html.escape(str(call_url), quote=True)
        call_line = (
            "<p>And if you would rather talk it through, I do short calls "
            f'with early users — <a href="{safe_call}" '
            'style="color:#1e4a7a;">grab a slot</a> any time.</p>'
        )
    body = f"""
      <p>Hi {_first_name(recipient)},</p>
      <p>Your first request just went through {label} — you're properly up
      and running now.</p>
      <p>Since that worked: {suggestion}.</p>
      <p>Anything that felt rough getting to this point, I want to hear
      about — just reply.</p>
      {call_line}
    """
    return _send_lifecycle(
        recipient["email"], recipient.get("name", ""),
        "That worked — you're up and running on EnConvert",
        body, recipient["user_id"],
    )


def founder_call(recipient: Dict, context: Dict, now: datetime) -> bool:
    """Context: call_url (Cal.com booking link, from FOUNDER_CALL_URL).
    Deliberately NO upgrade CTA, NO plan table, NO quota numbers — this is an
    invitation, not a pitch."""
    call_url = str(context.get("call_url") or getattr(config, "FOUNDER_CALL_URL", "") or "")
    if not call_url:
        logger.error("founder_call email skipped: no call_url configured")
        return False
    safe_call = html.escape(call_url, quote=True)
    body = f"""
      <p>Hi {_first_name(recipient)},</p>
      <p>I'm Het, the founder of EnConvert. You've been putting the platform
      to real use lately, and I'd genuinely like to hear how it's going.</p>
      <p>I'm doing short calls with the people using EnConvert most — twenty
      minutes, you tell me what's working and what's rough, I take notes and
      fix things. No agenda beyond that.</p>
      <p><a href="{safe_call}" style="color:#1e4a7a;">Pick a time that suits
      you</a>.</p>
      <p>Or just hit reply — that works too.</p>
    """
    return _send_lifecycle(
        recipient["email"], recipient.get("name", ""),
        "20 minutes? I'd like to hear how EnConvert is treating you",
        body, recipient["user_id"],
    )

"""
Email notification utility for async job completion.
Sends emails using Brevo API.
"""
import html
import os
import requests
from typing import Optional, List, Dict
import logging

logger = logging.getLogger(__name__)

BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
SENDER_EMAIL = "noreply@enconvert.com"
SENDER_NAME = "EnConvert"

# Old SMTP config (kept for reference)
# smtp_host = os.getenv("SMTP_HOST")
# smtp_port = int(os.getenv("SMTP_PORT", 587))
# smtp_username = os.getenv("SMTP_USERNAME")
# smtp_password = os.getenv("SMTP_PASSWORD")
# smtp_from_email = os.getenv("SMTP_FROM_EMAIL")
# smtp_from_name = os.getenv("SMTP_FROM_NAME", "EnConvert")


def send_job_completion_email(
    recipient_email: str,
    job_id: str,
    job_status: str,
    tasks: Optional[List[Dict]] = None,
    batch_id: Optional[str] = None
) -> bool:
    """
    Send email notification when async job completes via Brevo API.
    """
    if not BREVO_API_KEY:
        logger.error("BREVO_API_KEY not set")
        return False

    try:
        html_content = _build_email_html(job_id, job_status, tasks, batch_id)

        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "content-type": "application/json"},
            json={
                "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
                "to": [{"email": recipient_email}],
                "subject": f"Job Completion: {job_status.upper()}",
                "htmlContent": html_content,
            },
            timeout=(3.05, 10),
        )
        response.raise_for_status()
        logger.info(f"Job completion email sent to {recipient_email}")
        return True

    except Exception as e:
        logger.error(f"Failed to send job completion email: {e}")
        return False


def send_watcher_paused_email(
    recipient_email: str,
    watcher_id: str,
    url: str,
    error_message: str,
) -> bool:
    """Notify the project owner that a watcher auto-paused (Task I.1).

    Fired after three consecutive failed checks (the watcher stops rescheduling
    until reactivated via PATCH /v2/watch/{watcher_id}). Best-effort: returns
    False on any delivery problem; the caller never blocks on it.
    """
    if not BREVO_API_KEY:
        logger.error("BREVO_API_KEY not set")
        return False

    try:
        html_content = _build_watcher_paused_html(watcher_id, url, error_message)

        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "content-type": "application/json"},
            json={
                "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
                "to": [{"email": recipient_email}],
                "subject": "Your EnConvert watcher was paused",
                "htmlContent": html_content,
            },
            timeout=(3.05, 10),
        )
        response.raise_for_status()
        # Log the watcher id, not the recipient address (email is PII).
        logger.info(f"Watcher paused email sent for watcher {watcher_id}")
        return True

    except Exception as e:
        logger.error(f"Failed to send watcher paused email: {e}")
        return False


def _build_watcher_paused_html(
    watcher_id: str, url: str, error_message: str
) -> str:
    """Plain HTML body for the watcher auto-pause notification.

    Every interpolated value is HTML-escaped: ``url`` and ``error_message`` are
    user/page-derived (the SSRF scheme check does not strip HTML metacharacters),
    so unescaped interpolation would inject markup into the rendered email.
    """
    safe_watcher_id = html.escape(watcher_id)
    safe_url = html.escape(url[:200])
    safe_error = html.escape(error_message[:300])
    return f"""
    <html>
      <head>
        <style>
          body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
          .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
          .header {{ background-color: #dc3545; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }}
          .content {{ background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }}
          .info-row {{ margin: 10px 0; }}
          .label {{ font-weight: bold; color: #555; }}
          .value {{ color: #333; word-break: break-all; }}
          .footer {{ margin-top: 20px; font-size: 12px; color: #777; text-align: center; }}
        </style>
      </head>
      <body>
        <div class="container">
          <div class="header">
            <h2>Watcher paused</h2>
          </div>
          <div class="content">
            <div class="info-row">
              <span class="value">We paused one of your watchers after three
              consecutive failed checks. It will not run again until you
              reactivate it from your dashboard.</span>
            </div>
            <div class="info-row">
              <span class="label">Watcher:</span>
              <span class="value">{safe_watcher_id}</span>
            </div>
            <div class="info-row">
              <span class="label">URL:</span>
              <span class="value">{safe_url}</span>
            </div>
            <div class="info-row">
              <span class="label">Last error:</span>
              <span class="value">{safe_error}</span>
            </div>
            <div class="footer">
              <p>This is an automated notification from EnConvert API</p>
            </div>
          </div>
        </div>
      </body>
    </html>
    """


def send_watcher_change_email(
    recipient_email: str,
    watcher_id: str,
    url: str,
    similarity: Optional[float],
    changes: List[Dict],
    checked_at: str,
) -> bool:
    """Notify the project owner that a watched page changed (Task I.4).

    Best-effort: returns False on any delivery problem; the caller never blocks.
    Every interpolated value (url, change before/after) is HTML-escaped — it is
    untrusted page content.
    """
    if not BREVO_API_KEY:
        logger.error("BREVO_API_KEY not set")
        return False

    try:
        html_content = _build_watcher_change_html(
            url, similarity, changes, checked_at
        )
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "content-type": "application/json"},
            json={
                "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
                "to": [{"email": recipient_email}],
                "subject": "Change detected on a page you are watching",
                "htmlContent": html_content,
            },
            timeout=(3.05, 10),
        )
        response.raise_for_status()
        logger.info(f"Watcher change email sent for watcher {watcher_id}")
        return True

    except Exception as e:
        logger.error(f"Failed to send watcher change email: {e}")
        return False


# How many individual changes the email lists before summarising the remainder.
_MAX_EMAIL_CHANGES = 25


def _format_change_value(value: object) -> str:
    """Compact, HTML-escaped rendering of a change's before/after value."""
    if value is None:
        return "—"
    text = value if isinstance(value, str) else str(value)
    if len(text) > 160:
        text = text[:160] + "…"
    return html.escape(text)


def _build_watcher_change_html(
    url: str,
    similarity: Optional[float],
    changes: List[Dict],
    checked_at: str,
) -> str:
    """Plain HTML body for the watcher change notification (values escaped)."""
    origin = os.getenv("DASHBOARD_ORIGIN", "https://enconvert.com").rstrip("/")
    # Operator-set, but fall back + escape so a misconfigured value can never
    # break out of the href attribute or carry a non-http(s) scheme.
    if not origin.startswith(("https://", "http://")):
        origin = "https://enconvert.com"
    dashboard = html.escape(origin)
    safe_url = html.escape(url[:300])
    safe_checked = html.escape(checked_at)
    similarity_pct = f"{similarity * 100:.1f}%" if similarity is not None else "n/a"

    rows = []
    for change in changes[:_MAX_EMAIL_CHANGES]:
        section = html.escape(str(change.get("section", "")))
        kind = html.escape(str(change.get("kind", "")))
        field = html.escape(str(change.get("field") or change.get("key") or ""))
        before = _format_change_value(change.get("before"))
        after = _format_change_value(change.get("after"))
        rows.append(
            f"<tr><td>{section}</td><td>{kind}</td><td>{field}</td>"
            f"<td>{before}</td><td>{after}</td></tr>"
        )
    extra = len(changes) - _MAX_EMAIL_CHANGES
    if extra > 0:
        rows.append(
            f'<tr><td colspan="5" style="color:#777;">and {extra} more change(s)'
            f"</td></tr>"
        )

    return f"""
    <html>
      <head>
        <style>
          body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
          .container {{ max-width: 640px; margin: 0 auto; padding: 20px; }}
          .header {{ background-color: #0d6efd; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }}
          .content {{ background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }}
          .info-row {{ margin: 10px 0; }}
          .label {{ font-weight: bold; color: #555; }}
          .value {{ color: #333; word-break: break-all; }}
          table {{ width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 13px; }}
          th {{ background-color: #e9ecef; padding: 8px; text-align: left; }}
          td {{ padding: 8px; border-bottom: 1px solid #ddd; vertical-align: top; }}
          .btn {{ display: inline-block; margin-top: 18px; padding: 10px 18px; background: #0d6efd; color: #fff; text-decoration: none; border-radius: 5px; }}
          .footer {{ margin-top: 20px; font-size: 12px; color: #777; text-align: center; }}
        </style>
      </head>
      <body>
        <div class="container">
          <div class="header"><h2>A page you are watching changed</h2></div>
          <div class="content">
            <div class="info-row"><span class="label">URL:</span> <span class="value">{safe_url}</span></div>
            <div class="info-row"><span class="label">Checked:</span> <span class="value">{safe_checked}</span></div>
            <div class="info-row"><span class="label">Similarity to previous capture:</span> <span class="value">{similarity_pct}</span></div>
            <table>
              <thead><tr><th>Section</th><th>Change</th><th>Field</th><th>Before</th><th>After</th></tr></thead>
              <tbody>{''.join(rows)}</tbody>
            </table>
            <a class="btn" href="{dashboard}/dashboard/watch">View in dashboard</a>
            <div class="footer"><p>This is an automated notification from EnConvert API</p></div>
          </div>
        </div>
      </body>
    </html>
    """


def _build_email_html(
    job_id: str,
    job_status: str,
    tasks: Optional[List[Dict]],
    batch_id: Optional[str]
) -> str:
    """Build HTML email content."""
    status_color = "#28a745" if job_status.lower() == "success" else "#dc3545"
    status_emoji = "✅" if job_status.lower() == "success" else "❌"

    html = f"""
    <html>
      <head>
        <style>
          body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
          .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
          .header {{ background-color: {status_color}; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }}
          .content {{ background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }}
          .info-row {{ margin: 10px 0; }}
          .label {{ font-weight: bold; color: #555; }}
          .value {{ color: #333; }}
          .tasks-table {{ width: 100%; border-collapse: collapse; margin-top: 15px; }}
          .tasks-table th {{ background-color: #007bff; color: white; padding: 10px; text-align: left; }}
          .tasks-table td {{ padding: 10px; border-bottom: 1px solid #ddd; }}
          .status-success {{ color: #28a745; font-weight: bold; }}
          .status-failed {{ color: #dc3545; font-weight: bold; }}
          .footer {{ margin-top: 20px; font-size: 12px; color: #777; text-align: center; }}
        </style>
      </head>
      <body>
        <div class="container">
          <div class="header">
            <h2>{status_emoji} Job Completed: {job_status.upper()}</h2>
          </div>
          <div class="content">
            <div class="info-row">
              <span class="label">Job ID:</span>
              <span class="value">{job_id}</span>
            </div>
    """

    if batch_id:
        html += f"""
            <div class="info-row">
              <span class="label">Batch ID:</span>
              <span class="value">{batch_id}</span>
            </div>
        """

    html += f"""
            <div class="info-row">
              <span class="label">Status:</span>
              <span class="value" style="color: {status_color}; font-weight: bold;">{job_status.upper()}</span>
            </div>
            <div class="info-row">
              <span class="value">Your files are ready. Please download them from your dashboard.</span>
            </div>
    """

    # Add tasks table if bulk job
    if tasks:
        success_count = sum(1 for t in tasks if t.get("status") == "success")
        failed_count = len(tasks) - success_count

        html += f"""
            <div class="info-row" style="margin-top: 20px;">
              <span class="label">Total Tasks:</span>
              <span class="value">{len(tasks)}</span>
              <span class="value" style="margin-left: 15px;">
                <span class="status-success">{success_count} Successful</span> |
                <span class="status-failed">{failed_count} Failed</span>
              </span>
            </div>

            <table class="tasks-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>URL/Task</th>
                  <th>Status</th>
                  <th>Output</th>
                </tr>
              </thead>
              <tbody>
        """

        for idx, task in enumerate(tasks, 1):
            task_status = task.get("status", "unknown")
            status_class = "status-success" if task_status == "success" else "status-failed"
            url = task.get("url", "N/A")
            filename = task.get("filename", "N/A")

            html += f"""
                <tr>
                  <td>{idx}</td>
                  <td>{url[:50]}...</td>
                  <td class="{status_class}">{task_status.upper()}</td>
                  <td>{filename}</td>
                </tr>
            """

        html += """
              </tbody>
            </table>
        """

    html += """
            <div class="footer">
              <p>This is an automated notification from EnConvert API</p>
            </div>
          </div>
        </div>
      </body>
    </html>
    """

    return html


def send_api_key_unauthorized_domain_email(
    recipient_email: str,
    key_name: str,
    key_prefix: str,
    origin: str,
) -> bool:
    """Alert the project owner that a public API key was presented from an origin
    not on its allowed-domains list — a classic key-leak signal. Best-effort;
    the caller throttles this to once/24h per key so it cannot email-storm.
    Every interpolated value is HTML-escaped (origin/key name are attacker-set).
    """
    if not BREVO_API_KEY:
        logger.error("BREVO_API_KEY not set")
        return False

    try:
        safe_name = html.escape(key_name or "")
        safe_prefix = html.escape(key_prefix or "")
        safe_origin = html.escape((origin or "unknown")[:200])
        html_content = f"""
        <html><body style="font-family:Arial,sans-serif;color:#333;">
          <div style="max-width:600px;margin:0 auto;padding:20px;">
            <div style="background:#dc3545;color:#fff;padding:20px;text-align:center;border-radius:5px 5px 0 0;">
              <h2>Unauthorized use of an API key</h2>
            </div>
            <div style="background:#f9f9f9;padding:20px;border-radius:0 0 5px 5px;">
              <p>One of your public API keys was just used from a domain that is not on its allowed list. The request was rejected.</p>
              <p><strong>Key:</strong> {safe_name} ({safe_prefix}&hellip;)<br>
                 <strong>Blocked origin:</strong> {safe_origin}</p>
              <p>If you don't recognize this origin, the key may be embedded on a site you don't control — rotate it from your dashboard.</p>
              <div style="margin-top:20px;font-size:12px;color:#777;text-align:center;">
                <p>This is an automated notification from EnConvert API</p>
              </div>
            </div>
          </div>
        </body></html>
        """
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "content-type": "application/json"},
            json={
                "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
                "to": [{"email": recipient_email}],
                "subject": "Unauthorized use of your EnConvert API key",
                "htmlContent": html_content,
            },
            timeout=(3.05, 10),
        )
        response.raise_for_status()
        logger.info("Unauthorized-domain alert sent")
        return True
    except Exception as e:
        logger.error(f"Failed to send unauthorized-domain alert: {e}")
        return False


def send_quota_reached_email(
    recipient_email: str,
    plan_slug: str,
    used: int,
    limit: int,
) -> bool:
    """Notify the project owner their monthly conversion quota hit 100%.
    Best-effort; the caller throttles this to once/24h per project."""
    if not BREVO_API_KEY:
        logger.error("BREVO_API_KEY not set")
        return False

    try:
        safe_plan = html.escape(plan_slug or "your plan")
        html_content = f"""
        <html><body style="font-family:Arial,sans-serif;color:#333;">
          <div style="max-width:600px;margin:0 auto;padding:20px;">
            <div style="background:#d29922;color:#fff;padding:20px;text-align:center;border-radius:5px 5px 0 0;">
              <h2>Monthly conversion limit reached</h2>
            </div>
            <div style="background:#f9f9f9;padding:20px;border-radius:0 0 5px 5px;">
              <p>Your project has used <strong>{int(used)}</strong> of its <strong>{int(limit)}</strong> monthly conversions on the {safe_plan} plan.</p>
              <p>Further conversions are blocked (HTTP 402) until your quota resets or you upgrade.</p>
              <div style="text-align:center;margin:18px 0;">
                <a href="https://www.enconvert.com/dashboard/billing" style="display:inline-block;padding:10px 18px;background:#143459;color:#fff;text-decoration:none;border-radius:5px;">Upgrade plan</a>
              </div>
              <div style="margin-top:20px;font-size:12px;color:#777;text-align:center;">
                <p>This is an automated notification from EnConvert API</p>
              </div>
            </div>
          </div>
        </body></html>
        """
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "content-type": "application/json"},
            json={
                "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
                "to": [{"email": recipient_email}],
                "subject": "You've hit your monthly conversion limit – EnConvert",
                "htmlContent": html_content,
            },
            timeout=(3.05, 10),
        )
        response.raise_for_status()
        logger.info("Quota-reached alert sent")
        return True
    except Exception as e:
        logger.error(f"Failed to send quota-reached alert: {e}")
        return False


# ---------------------------------------------------------------------------
# Subscription lifecycle emails (services/subscription_emails.py, run by the
# enconvert-billing-rotation systemd timer). Same conventions as above: sync,
# best-effort, return bool, never raise, HTML-escape every interpolated value.
# Amount phrasing rules: plan prices are DISPLAY COPY ("your plan price",
# "estimated") because PayPal defines the real charge (migration 018 note);
# overage receipt amounts ARE authoritative (what the gateway captured).
# ---------------------------------------------------------------------------


def _send_brevo(recipient_email: str, subject: str, html_content: str) -> bool:
    """POST one transactional email to Brevo. Shared by the senders below."""
    response = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "content-type": "application/json"},
        json={
            "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
            "to": [{"email": recipient_email}],
            "subject": subject,
            "htmlContent": html_content,
        },
        timeout=(3.05, 10),
    )
    response.raise_for_status()
    return True


def _billing_shell(header: str, header_color: str, body_html: str) -> str:
    """Shared layout for the subscription lifecycle emails below.

    ``body_html`` must already be escaped by the caller; everything this
    function adds is static markup.
    """
    return f"""
    <html><body style="font-family:Arial,sans-serif;line-height:1.6;color:#333;">
      <div style="max-width:600px;margin:0 auto;padding:20px;">
        <div style="background:{header_color};color:#fff;padding:20px;text-align:center;border-radius:5px 5px 0 0;">
          <h2 style="margin:0;">{header}</h2>
        </div>
        <div style="background:#f9f9f9;padding:20px;border-radius:0 0 5px 5px;">
          {body_html}
          <div style="text-align:center;margin:18px 0;">
            <a href="https://www.enconvert.com/dashboard/billing" style="display:inline-block;padding:10px 18px;background:#143459;color:#fff;text-decoration:none;border-radius:5px;">Manage billing</a>
          </div>
          <div style="margin-top:20px;font-size:12px;color:#777;text-align:center;">
            <p>This is an automated notification from EnConvert API</p>
          </div>
        </div>
      </div>
    </body></html>
    """


def send_trial_ending_email(
    recipient_email: str,
    plan_name: str,
    trial_end_str: str,
    days_left: int,
) -> bool:
    """Remind the owner that their trial converts to a paid plan soon."""
    if not BREVO_API_KEY:
        logger.error("BREVO_API_KEY not set")
        return False
    try:
        safe_plan = html.escape(plan_name)
        safe_end = html.escape(trial_end_str)
        day_word = "day" if days_left == 1 else "days"
        body = f"""
          <p>Your <strong>{safe_plan}</strong> plan trial ends in
          <strong>{int(days_left)} {day_word}</strong> (on {safe_end}).</p>
          <p>When the trial ends, your plan price will be charged to your
          payment method automatically. If you do not want to continue, you
          can cancel any time before then from your dashboard.</p>
        """
        _send_brevo(
            recipient_email,
            "Your EnConvert trial ends soon",
            _billing_shell("Trial ending soon", "#143459", body),
        )
        logger.info("Trial-ending reminder sent")
        return True
    except Exception as e:
        logger.error(f"Failed to send trial-ending reminder: {e}")
        return False


def send_storage_lapse_warning_email(
    recipient_email: str,
    project_name: str,
    lapse_date_str: str,
    storage_used_str: str,
) -> bool:
    """Warn the owner that a cancelled storage add-on is about to lapse."""
    if not BREVO_API_KEY:
        logger.error("BREVO_API_KEY not set")
        return False
    try:
        safe_project = html.escape(project_name[:100])
        safe_date = html.escape(lapse_date_str)
        safe_used = html.escape(storage_used_str)
        body = f"""
          <p>The storage add-on for your project <strong>{safe_project}</strong>
          was cancelled and expires on <strong>{safe_date}</strong>.</p>
          <p>You currently have <strong>{safe_used}</strong> stored. After the
          add-on lapses, stored files above your plan's base allowance are no
          longer covered - download anything you need or re-subscribe before
          then.</p>
        """
        _send_brevo(
            recipient_email,
            "Your EnConvert storage add-on expires soon",
            _billing_shell("Storage add-on expiring", "#dc3545", body),
        )
        logger.info("Storage-lapse warning sent")
        return True
    except Exception as e:
        logger.error(f"Failed to send storage-lapse warning: {e}")
        return False


def send_overage_receipt_email(
    recipient_email: str,
    amount: str,
    currency: str,
    charged_on_str: str,
    plan_name: str,
    overage_conversions: Optional[int] = None,
) -> bool:
    """Receipt for a captured pay-as-you-go overage charge.

    ``amount``/``currency`` are authoritative: they are exactly what the
    gateway asked PayPal to capture (ch_payment_history.amount_value).
    """
    if not BREVO_API_KEY:
        logger.error("BREVO_API_KEY not set")
        return False
    try:
        safe_amount = html.escape(amount)
        safe_currency = html.escape(currency)
        safe_date = html.escape(charged_on_str)
        safe_plan = html.escape(plan_name)
        count_line = (
            f"<p>Overage conversions billed: <strong>{int(overage_conversions)}</strong></p>"
            if overage_conversions is not None
            else ""
        )
        body = f"""
          <p>We charged <strong>{safe_amount} {safe_currency}</strong> on
          {safe_date} for pay-as-you-go conversions used beyond your
          <strong>{safe_plan}</strong> plan's monthly quota.</p>
          {count_line}
          <p>The full payment history is available in your dashboard.</p>
        """
        _send_brevo(
            recipient_email,
            "Receipt: EnConvert overage charge",
            _billing_shell("Overage charge receipt", "#143459", body),
        )
        logger.info("Overage receipt sent")
        return True
    except Exception as e:
        logger.error(f"Failed to send overage receipt: {e}")
        return False


def send_renewal_notice_email(
    recipient_email: str,
    plan_name: str,
    period_start_str: str,
    period_end_str: str,
    conversion_limit: int,
) -> bool:
    """Notify the owner that a new billing period started (quota reset)."""
    if not BREVO_API_KEY:
        logger.error("BREVO_API_KEY not set")
        return False
    try:
        safe_plan = html.escape(plan_name)
        safe_start = html.escape(period_start_str)
        safe_end = html.escape(period_end_str)
        body = f"""
          <p>A new billing period started for your <strong>{safe_plan}</strong>
          plan.</p>
          <p>Period: <strong>{safe_start}</strong> to <strong>{safe_end}</strong></p>
          <p>Your monthly quota of <strong>{int(conversion_limit)}</strong>
          conversions has been reset. Your plan price is billed by PayPal on
          your regular subscription schedule.</p>
        """
        _send_brevo(
            recipient_email,
            "Your EnConvert plan renewed - quota reset",
            _billing_shell("Plan renewed", "#143459", body),
        )
        logger.info("Renewal notice sent")
        return True
    except Exception as e:
        logger.error(f"Failed to send renewal notice: {e}")
        return False


def send_upcoming_charge_email(
    recipient_email: str,
    charge_date_str: str,
    line_items: List[Dict],
) -> bool:
    """Remind the owner ~2 days before a charge hits their payment method.

    ``line_items``: [{"label": str, "amount": str, "currency": str}, ...].
    Amounts are estimates ("your plan price" / accrued overage so far); the
    real charge is made by PayPal on its own schedule, so the copy says
    "on or around" the date.
    """
    if not BREVO_API_KEY:
        logger.error("BREVO_API_KEY not set")
        return False
    try:
        safe_date = html.escape(charge_date_str)
        rows = "".join(
            "<tr>"
            f"<td style=\"padding:6px 10px;border-bottom:1px solid #ddd;\">{html.escape(str(item.get('label', '')))}</td>"
            f"<td style=\"padding:6px 10px;border-bottom:1px solid #ddd;text-align:right;\">{html.escape(str(item.get('amount', '')))} {html.escape(str(item.get('currency', 'USD')))}</td>"
            "</tr>"
            for item in line_items
        )
        body = f"""
          <p>This is a reminder that your payment method will be charged
          <strong>on or around {safe_date}</strong> for:</p>
          <table style="width:100%;border-collapse:collapse;margin:12px 0;">
            {rows}
          </table>
          <p>Amounts shown are estimates based on your current plan and usage;
          the exact charge is processed by PayPal. To make changes, visit your
          dashboard before the renewal date.</p>
        """
        _send_brevo(
            recipient_email,
            "Upcoming charge on your EnConvert subscription",
            _billing_shell("Upcoming charge", "#143459", body),
        )
        logger.info("Upcoming-charge reminder sent")
        return True
    except Exception as e:
        logger.error(f"Failed to send upcoming-charge reminder: {e}")
        return False

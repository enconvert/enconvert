import os

ENVIROMENT = os.getenv("ENVIRONMENT")
TURNSTILE_SITE_KEY = os.getenv("TURNSTILE_SITE_KEY", "1x00000000000000000000AA")
GATEWAY_DOMAIN = os.getenv("GATEWAY_DOMAIN", "http://localhost:8000")
WIDGET_ORIGIN = os.getenv("WIDGET_ORIGIN", "http://localhost:5173")
DASHBOARD_ORIGIN = os.getenv("DASHBOARD_ORIGIN", "http://localhost:5173")
BACKEND_ORIGIN = os.getenv("BACKEND_ORIGIN", "http://localhost:8000")
DATABASE_URL = os.getenv("DATABASE_URL")


# ── API-key usage stamping (migration 031: ch_api_keys.first_used_at /
# last_used_at / first_used_surface, written by auth/usage_stamp.py) ─────────
# API_KEY_USAGE_STAMP_ENABLED: default ON; "0"/"false"/"no" disables the
# stamp entirely (same truthiness parsing as main._worker_enabled).
API_KEY_USAGE_STAMP_ENABLED = (
    os.getenv("API_KEY_USAGE_STAMP_ENABLED", "1").strip().lower()
    not in ("0", "false", "no")
)
# Minimum seconds between last_used_at writes for the same key (in-process
# throttle; first_used_at is COALESCE-protected in SQL so the throttle only
# bounds write frequency, never correctness). Default 3600 (1/hour/key).
API_KEY_LAST_USED_THROTTLE_SECONDS = int(
    os.getenv("API_KEY_LAST_USED_THROTTLE_SECONDS", "3600")
)


# ── Onboarding lifecycle emails (services/lifecycle_emails.py, run by the
# enconvert-lifecycle-emails systemd timer via scripts/ops/run_lifecycle_emails.py).
# All flags parse like _worker_enabled / API_KEY_USAGE_STAMP_ENABLED above. ──
# Master switch: default OFF so a fresh deploy can never email anyone until
# the operator deliberately enables it.
LIFECYCLE_EMAILS_ENABLED = (
    os.getenv("LIFECYCLE_EMAILS_ENABLED", "0").strip().lower()
    not in ("0", "false", "no", "")
)
# Dry-run: default ON — claims and logs are written but no Brevo call is made.
# Both this AND the master switch must be flipped for real sends.
LIFECYCLE_DRY_RUN = (
    os.getenv("LIFECYCLE_DRY_RUN", "1").strip().lower()
    not in ("0", "false", "no")
)
# ISO date (e.g. "2026-08-15"): only users created ON or AFTER this instant
# are lifecycle candidates. REQUIRED when the system is enabled — there is
# deliberately no default, so the runner refuses to run rather than sweep the
# whole historical user base on a misconfigured deploy.
LIFECYCLE_EPOCH = os.getenv("LIFECYCLE_EPOCH")
# Per-run claim budget across all stages (a claim = one ch_email_log row).
MAX_LIFECYCLE_SENDS_PER_TICK = int(os.getenv("MAX_LIFECYCLE_SENDS_PER_TICK", "25"))
# Sender identity: personal founder mail, distinct from the transactional
# noreply@ identity in utils/email_notifier.py. Reply-To is on the MAIN
# domain (enconvert.com) on purpose — replies land in the founder inbox even
# though the sending domain is getenconvert.com.
LIFECYCLE_FROM_EMAIL = os.getenv("LIFECYCLE_FROM_EMAIL", "het@getenconvert.com")
LIFECYCLE_FROM_NAME = os.getenv("LIFECYCLE_FROM_NAME", "Het Dave")
LIFECYCLE_REPLY_TO = os.getenv("LIFECYCLE_REPLY_TO", "het@enconvert.com")
# HMAC secret for the stateless unsubscribe token (utils/unsub_token.py).
# No default: token minting raises and parsing rejects when unset (fail closed).
LIFECYCLE_UNSUB_SECRET = os.getenv("LIFECYCLE_UNSUB_SECRET")
LIFECYCLE_UNSUB_URL = os.getenv(
    "LIFECYCLE_UNSUB_URL", "https://api.enconvert.com/email/unsubscribe"
)
# Shared secret Brevo sends back in the X-Brevo-Auth header on webhook posts.
# No default: the webhook processes nothing when unset (fail closed).
BREVO_WEBHOOK_SECRET = os.getenv("BREVO_WEBHOOK_SECRET")
# Cal.com booking link for the founder-call email. No default: the
# founder_call stage skips cleanly when unset.
FOUNDER_CALL_URL = os.getenv("FOUNDER_CALL_URL")
FOUNDER_CALL_SLOTS_PER_WEEK = int(os.getenv("FOUNDER_CALL_SLOTS_PER_WEEK", "5"))


# Per-project request-rate limits, by plan slug and API-key type. These are the
# short-window FAIRNESS limits enforced by rate_limiting/limiter.py (HTTP 429),
# and are SEPARATE from the monthly conversion quotas in ch_plans (HTTP 402).
# Slugs MUST match the real ch_plans slugs (free/starter/pro/business); unknown
# slugs fall back to "free" in the limiter. A "public" (pk_) key is shared
# across a customer's browser visitors, so its limits are tighter than a
# "private" (server-side sk_) key.
RATE_LIMITS = {
    "free": {
        "private": {
            "per_minute": 5,
            "per_hour": 100,
            "per_day": 1000,
        },
        "public": {
            "per_minute": 5,
            "per_hour": 50,
            "per_day": 500,
        },
    },
    "starter": {
        "private": {
            "per_minute": 50,
            "per_hour": 1000,
            "per_day": 10000,
        },
        "public": {
            "per_minute": 25,
            "per_hour": 500,
            "per_day": 5000,
        },
    },
    "pro": {
        "private": {
            "per_minute": 150,
            "per_hour": 3000,
            "per_day": 30000,
        },
        "public": {
            "per_minute": 75,
            "per_hour": 1500,
            "per_day": 15000,
        },
    },
    "business": {
        "private": {
            "per_minute": 300,
            "per_hour": 6000,
            "per_day": 600000,
        },
        "public": {
            "per_minute": 150,
            "per_hour": 3000,
            "per_day": 30000,
        },
    },
}

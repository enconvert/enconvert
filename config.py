import os

ENVIROMENT = os.getenv("ENVIRONMENT")
TURNSTILE_SITE_KEY = os.getenv("TURNSTILE_SITE_KEY", "1x00000000000000000000AA")
GATEWAY_DOMAIN = os.getenv("GATEWAY_DOMAIN", "http://localhost:8000")
WIDGET_ORIGIN = os.getenv("WIDGET_ORIGIN", "http://localhost:5173")
DASHBOARD_ORIGIN = os.getenv("DASHBOARD_ORIGIN", "http://localhost:5173")
BACKEND_ORIGIN = os.getenv("BACKEND_ORIGIN", "http://localhost:8000")
DATABASE_URL = os.getenv("DATABASE_URL")



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

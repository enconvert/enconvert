import logging
from datetime import datetime, timezone

logger = logging.getLogger("conversion-api-gateway")
logger.setLevel(logging.INFO)

# -----------------------------
# API Request Logging
# -----------------------------
def log_request(user_id: str, endpoint: str, status_code: int, duration: float, client_ip: str):
    """Log API request"""
    logger.info(
        "API request",
        extra={
            "json_fields": {
                "event_type": "api_request",
                "user_id": user_id,
                "endpoint": endpoint,
                "status_code": status_code,
                "duration_seconds": duration,
                "client_ip": client_ip,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        }
    )

# -----------------------------
# Security Event Logging
# -----------------------------
def log_security_event(event_type: str, details: dict):
    """Log security events"""
    logger.warning(
        "Security event",
        extra={
            "json_fields": {
                "event_type": event_type,
                "details": details,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        }
    )


# -----------------------------
# Usage Reconciliation Logging
# -----------------------------
def log_reconciliation_event(details: dict):
    """Log one usage-ledger drift discrepancy (migration 016).

    Emitted by scripts/ops/reconcile_usage_ledger.py for every period
    whose aggregate counter disagrees with its ledger sum. WARNING level
    on purpose: zero of these night after night is the expected steady
    state, so any occurrence should stand out in the journal.
    """
    logger.warning(
        "Usage reconciliation drift",
        extra={
            "json_fields": {
                "event_type": "usage_reconciliation_drift",
                "details": details,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        }
    )

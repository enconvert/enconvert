"""Local URL threat policy — a self-contained denylist with an audit trail.

This is the auditable, no-external-dependency counterpart to a hosted URL-
reputation feed. It NEVER makes a network call (so the target URL is never
transmitted — ZDR-safe) and is checked at the same choke point as the IP-range
SSRF guard (``url_safety.assert_public_http_url``), so it covers every fetch
path: perceive, discover, ingest, watch, batch, webhook delivery, and the V1
converters.

Policy is loaded once from environment variables and an optional JSON file, and
every block is recorded to a bounded in-memory audit ring plus a structured log
line (host only — never the full URL, which can carry sensitive query strings).
FireCrawl's threat protection is a closed hosted feed with no audit trail; this
is deliberately local, inspectable, and self-hostable.

Config (all optional; an empty policy blocks nothing):
  THREAT_POLICY_ENABLED      "1"/"0" (default "1"). "0" disables all checks.
  THREAT_BLOCKED_DOMAINS     comma-separated hostnames; blocks the domain and
                             its subdomains (e.g. "evil.com" blocks a.evil.com).
  THREAT_BLOCKED_TLDS        comma-separated TLDs (e.g. "zip,mov,internal").
  THREAT_POLICY_FILE         path to a JSON file:
                               {"blocked_domains": [...], "blocked_tlds": [...],
                                "blocked_patterns": ["regex", ...]}
                             (blocked_patterns are matched against the HOST only.)
  THREAT_POLICY_FAIL_CLOSED  "1" to reject on a policy-evaluation error
                             (default "0" — fail open so a broken policy file
                             never takes the fetch path down; a MATCH always
                             blocks regardless of this flag).
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

from fastapi import HTTPException

logger = logging.getLogger(__name__)

_AUDIT_MAX = 200
# Bounded audit ring of recent blocks (dashboards / support / tests read this).
_audit: deque[dict] = deque(maxlen=_AUDIT_MAX)


@dataclass(frozen=True)
class _Policy:
    enabled: bool
    blocked_domains: frozenset[str]
    blocked_tlds: frozenset[str]
    blocked_patterns: tuple[re.Pattern, ...]
    fail_closed: bool


def _env_list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip().lower().lstrip(".") for item in raw.split(",") if item.strip()]


def _load_policy() -> _Policy:
    enabled = os.getenv("THREAT_POLICY_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )
    domains = set(_env_list("THREAT_BLOCKED_DOMAINS"))
    tlds = set(_env_list("THREAT_BLOCKED_TLDS"))
    patterns: list[str] = []

    path = os.getenv("THREAT_POLICY_FILE", "").strip()
    if path:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            domains.update(
                str(d).strip().lower().lstrip(".")
                for d in data.get("blocked_domains", [])
                if str(d).strip()
            )
            tlds.update(
                str(t).strip().lower().lstrip(".")
                for t in data.get("blocked_tlds", [])
                if str(t).strip()
            )
            patterns.extend(
                str(p) for p in data.get("blocked_patterns", []) if str(p).strip()
            )
        except (OSError, ValueError) as exc:
            logger.error("threat_policy: could not load %s: %s", path, exc)

    compiled: list[re.Pattern] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error as exc:
            logger.error("threat_policy: bad blocked_pattern %r: %s", pattern, exc)

    fail_closed = os.getenv("THREAT_POLICY_FAIL_CLOSED", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )
    return _Policy(
        enabled=enabled,
        blocked_domains=frozenset(domains),
        blocked_tlds=frozenset(tlds),
        blocked_patterns=tuple(compiled),
        fail_closed=fail_closed,
    )


_policy: Optional[_Policy] = None


def _get_policy() -> _Policy:
    global _policy
    if _policy is None:
        _policy = _load_policy()
    return _policy


def reload() -> None:
    """Re-read policy from the environment/file (tests + config changes)."""
    global _policy
    _policy = _load_policy()


def audit_log() -> list[dict]:
    """Recent blocks, newest last (bounded)."""
    return list(_audit)


def _record_block(host: str, rule: str, detail: str) -> None:
    entry = {
        "host": host,
        "rule": rule,
        "detail": detail,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    _audit.append(entry)
    # Host only — never the full URL (query strings can carry secrets/PII).
    logger.warning("threat_policy block: host=%s rule=%s (%s)", host, rule, detail)


def check_host(host: str) -> Optional[dict]:
    """Return a block record if ``host`` is denied by policy, else None.

    Pure evaluation (no side effects) — callers that want the audit trail use
    ``assert_allowed``. A policy-evaluation error is swallowed and returns None
    (fail open) unless THREAT_POLICY_FAIL_CLOSED is set.
    """
    policy = _get_policy()
    if not policy.enabled or not host:
        return None
    host = host.lower().rstrip(".")
    try:
        # Exact domain or subdomain of a blocked domain.
        for domain in policy.blocked_domains:
            if host == domain or host.endswith("." + domain):
                return {"rule": "domain", "detail": domain}
        # Blocked TLD (last label).
        last_label = host.rsplit(".", 1)[-1]
        if last_label in policy.blocked_tlds:
            return {"rule": "tld", "detail": last_label}
        # Regex patterns against the host.
        for pattern in policy.blocked_patterns:
            if pattern.search(host):
                return {"rule": "pattern", "detail": pattern.pattern}
    except Exception:  # noqa: BLE001 — a policy bug must not crash the fetch path
        logger.exception("threat_policy: evaluation error for host")
        if policy.fail_closed:
            return {"rule": "error", "detail": "policy evaluation failed (fail-closed)"}
        return None
    return None


def assert_allowed(url: str, host: Optional[str] = None) -> None:
    """Raise HTTPException(400) if ``url``'s host is denied by policy.

    Audited on block. ``host`` may be passed to avoid re-parsing when the
    caller already has it (url_safety does).
    """
    if host is None:
        host = urlsplit(url).hostname or ""
    verdict = check_host(host)
    if verdict is not None:
        _record_block(host, verdict["rule"], verdict["detail"])
        raise HTTPException(
            status_code=400,
            detail="This URL is blocked by the site's threat policy.",
        )

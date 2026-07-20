"""Per-project webhook signing secret management (Task H.8).

The secret backs HMAC-SHA256 signing of V2 completion webhooks
(``utils/callback_notifier``). It lives on ``ch_projects.webhook_secret``
(migration 015) so a single project-scoped key signs every /v2/ingest and
(from Sprint I.4) /v2/watch delivery.

Lifecycle:
* ``get_or_create_webhook_secret`` — read the secret, generating + persisting
  one the first time a project needs to sign a webhook. Race-safe: the create
  is a conditional ``UPDATE ... WHERE webhook_secret IS NULL`` so two
  concurrent first-deliveries converge on one value (the re-read returns the
  winner, not the loser's discarded candidate).
* ``rotate_webhook_secret`` — replace the secret (dashboard "rotate" action /
  leaked-key response). Old signatures stop verifying immediately.

Secrets are opaque, url-safe, ``whsec_``-prefixed tokens. The prefix mirrors
the ``pk_``/``sk_`` API-key convention so a leaked value is greppable.
"""

from __future__ import annotations

import secrets
from typing import Optional

from sqlalchemy import update
from sqlmodel import select

from models import Project
from utils.postgres import get_db

WEBHOOK_SECRET_PREFIX = "whsec_"
# token_urlsafe(32) -> ~43 chars; + 6-char prefix -> ~49. VARCHAR(80) (migration
# 015) leaves headroom and never truncates.
_SECRET_ENTROPY_BYTES = 32


def _generate_secret() -> str:
    """A fresh, cryptographically-random signing secret."""
    return WEBHOOK_SECRET_PREFIX + secrets.token_urlsafe(_SECRET_ENTROPY_BYTES)


def get_webhook_secret(project_id: int) -> Optional[str]:
    """Return the project's signing secret, or None if none is set / no project."""
    db = get_db()
    try:
        project = db.exec(
            select(Project).where(Project.id == project_id)
        ).first()
        return project.webhook_secret if project else None
    finally:
        db.close()


def get_or_create_webhook_secret(project_id: int) -> Optional[str]:
    """Return the project's signing secret, generating one on first use.

    Returns None only when the project row does not exist. The create is a
    conditional UPDATE (``WHERE webhook_secret IS NULL``) so concurrent first
    deliveries are race-safe: whoever wins the UPDATE sets the value, and the
    final re-read returns that single winning secret to every caller.
    """
    db = get_db()
    try:
        project = db.exec(
            select(Project).where(Project.id == project_id)
        ).first()
        if project is None:
            return None
        if project.webhook_secret:
            return project.webhook_secret

        candidate = _generate_secret()
        db.execute(
            update(Project)
            .where(
                Project.id == project_id,
                Project.webhook_secret.is_(None),  # type: ignore[union-attr]
            )
            .values(webhook_secret=candidate)
        )
        db.commit()

        # Re-read in the SAME session so concurrent creators all return the
        # committed winner, not their own discarded candidate.
        db.expire_all()
        return db.exec(
            select(Project.webhook_secret).where(Project.id == project_id)
        ).first()
    finally:
        db.close()


def rotate_webhook_secret(project_id: int) -> Optional[str]:
    """Replace the project's signing secret with a fresh one; return it.

    Returns None if the project does not exist. Any signature computed with the
    previous secret stops verifying the moment this commits.
    """
    candidate = _generate_secret()
    db = get_db()
    try:
        result = db.execute(
            update(Project)
            .where(Project.id == project_id)
            .values(webhook_secret=candidate)
        )
        db.commit()
        if not result.rowcount:
            return None
    finally:
        db.close()
    return candidate

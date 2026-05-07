"""Helpers for writing append-only audit log entries."""
from typing import Optional

from .db import AuditEntry, SessionLocal


def write_audit(
    event_id: str,
    action: str,
    *,
    ubid: Optional[str] = None,
    source: str = "sws",
    target: Optional[str] = None,
    status: str = "ok",
    payload: Optional[dict] = None,
    error: Optional[str] = None,
):
    """Append one row. Always succeeds (best-effort by design — audit
    failures must NEVER block business propagation)."""
    try:
        with SessionLocal() as db:
            db.add(
                AuditEntry(
                    event_id=event_id,
                    ubid=ubid,
                    source=source,
                    target=target,
                    action=action,
                    status=status,
                    payload=payload,
                    error=error,
                )
            )
            db.commit()
    except Exception:
        # Never let audit failure cascade. In production you'd ship this
        # to a dead-letter audit topic and page on-call.
        pass

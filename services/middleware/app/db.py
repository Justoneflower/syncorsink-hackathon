"""
Middleware DB schema. Three core tables:

1. routing_index — UBID → list of (dept, dept_native_id) tuples.
   This is what makes "which depts care about UBID-X" a fast lookup.
2. audit_log — append-only record of every propagation attempt.
   Used by the trace CLI to answer "what happened to UBID-X".
3. dead_letter — events whose propagation failed after all retries.
   In v2 these get a manual replay UI.
"""
import os
from datetime import datetime, timezone

from sqlalchemy import (
    create_engine,
    Column,
    String,
    Integer,
    JSON,
    DateTime,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.environ["DATABASE_URL"]
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False)
Base = declarative_base()


class RoutingEntry(Base):
    """One row per (UBID, dept) pair. Built from PAN-based seeding;
    in production this would be reconciled nightly + updated on every
    new dept registration event."""
    __tablename__ = "routing_index"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ubid = Column(String, nullable=False, index=True)
    dept = Column(String, nullable=False)  # "factories" | "shops" | "kspcb"
    dept_native_id = Column(String, nullable=False)  # license_no, reg_no, consent_id...
    pan = Column(String, nullable=False, index=True)  # for PAN-based fallback lookup
    confidence = Column(String, default="exact")  # "exact" | "fuzzy" | "manual"
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (UniqueConstraint("ubid", "dept", name="uq_ubid_dept"),)


class AuditEntry(Base):
    """Every propagation, retry, conflict, and resolution lands here.
    Append-only. Queryable by event_id or UBID for end-to-end traces."""
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String, nullable=False, index=True)
    ubid = Column(String, index=True)
    source = Column(String, nullable=False)  # "sws" | "factories" | "shops"
    target = Column(String)  # destination dept, or null for ingest events
    action = Column(String, nullable=False)
    # ^ "ingest" | "lookup" | "translate" | "write" | "retry" | "dlq" | "conflict" | "policy"
    status = Column(String)  # "ok" | "error" | "skipped"
    payload = Column(JSON)
    error = Column(Text)
    occurred_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )


class DeadLetter(Base):
    """Events that failed propagation after all retries."""
    __tablename__ = "dead_letter"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String, nullable=False, unique=True)
    ubid = Column(String, index=True)
    target = Column(String)
    payload = Column(JSON)
    last_error = Column(Text)
    attempts = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ConflictReview(Base):
    """Conflicts requiring human resolution (policy=human_escalation).
    An officer reviews each entry, picks a winner, and the middleware
    re-applies the chosen value."""
    __tablename__ = "conflict_review"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String, nullable=False, index=True)
    ubid = Column(String, nullable=False, index=True)
    field = Column(String, nullable=False)
    incoming_source = Column(String, nullable=False)
    incoming_value = Column(JSON)
    competing_source = Column(String, nullable=False)
    competing_value = Column(JSON)
    status = Column(String, default="pending")  # "pending" | "resolved" | "dismissed"
    resolution = Column(String)  # "incoming" | "competing" | "manual"
    resolved_by = Column(String)
    resolved_at = Column(DateTime)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


def init_schema():
    Base.metadata.create_all(engine)

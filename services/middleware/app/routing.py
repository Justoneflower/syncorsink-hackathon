"""
Routing index — answers "which depts have records for this UBID?".

In production this would be:
- Built nightly via full reconciliation scan
- Updated event-driven on every new dept registration
- Backed by ML entity resolution for legacy businesses missing UBID
  (the L1 failure mode from the deck)

In the prototype we build it via PAN-based join during seed, since every
mock dept stores PAN. Real depts may not — that's where ML comes in (v2).
"""
from typing import List

from .db import RoutingEntry, SessionLocal


def lookup(ubid: str) -> List[RoutingEntry]:
    """Returns all dept entries for a given UBID."""
    with SessionLocal() as db:
        return db.query(RoutingEntry).filter(RoutingEntry.ubid == ubid).all()


def upsert(ubid: str, dept: str, dept_native_id: str, pan: str, confidence: str = "exact"):
    """Add or update a routing entry. Called during seed and on new
    dept registration events (v2)."""
    with SessionLocal() as db:
        existing = (
            db.query(RoutingEntry)
            .filter(RoutingEntry.ubid == ubid, RoutingEntry.dept == dept)
            .first()
        )
        if existing:
            existing.dept_native_id = dept_native_id
            existing.pan = pan
            existing.confidence = confidence
        else:
            db.add(
                RoutingEntry(
                    ubid=ubid,
                    dept=dept,
                    dept_native_id=dept_native_id,
                    pan=pan,
                    confidence=confidence,
                )
            )
        db.commit()


def all_entries():
    with SessionLocal() as db:
        return db.query(RoutingEntry).all()

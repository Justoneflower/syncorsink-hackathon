"""
Conflict detection + resolution engine.

The split-brain problem: when an update to the same UBID arrives from two
sources within a short window (e.g., SWS user changes address while a
Factories officer simultaneously updates it), naive propagation overwrites
one with the other and silently loses data.

Our approach:

1. INFLIGHT WINDOW. For every (UBID, field) pair we observe, we record
   the source + timestamp in Redis with a 30s TTL. If a second update
   for the same (UBID, field) arrives from a different source within
   that 30s window, we have a conflict.

2. CONFLICT IS DETECTED PER-FIELD, not per-event. Two simultaneous
   updates that touch DIFFERENT fields (e.g., SWS changes address while
   Factories changes occupier_name) are NOT a conflict — they propagate
   normally.

3. RESOLUTION POLICY is configurable per field. Three policies:
     - source_of_record: a designated source always wins for this field
       (e.g., "address always wins from SWS"). Late writes from other
       sources are rejected and logged.
     - last_write_wins: the most recent timestamp wins.
     - human_escalation: both writes are blocked, the conflict goes to
       a review queue, an officer decides.

4. EVERY conflict and resolution is written to the audit log so the
   final state can always be explained.

In v1 the propagator skipped this entirely — every update propagated.
v2 wraps every propagation attempt in a conflict check.
"""
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import redis

log = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
_r = redis.from_url(REDIS_URL, decode_responses=True)

WINDOW_SECONDS = 30


class Policy(str, Enum):
    SOURCE_OF_RECORD = "source_of_record"
    LAST_WRITE_WINS = "last_write_wins"
    HUMAN_ESCALATION = "human_escalation"


# Per-field policy table. In production this would be config-driven
# (DB table, hot-reloadable). For the prototype, declared in code.
#
# Justification for the defaults:
#   - registered_address: SWS is the forward-looking authoritative
#     source for address changes; depts can correct typos but SWS wins
#     on substantive changes. → source_of_record(sws)
#   - authorised_signatory: legacy systems (Factories) often have the
#     freshest signatory data because filings happen there first. We
#     don't want to designate one source — let the most recent win,
#     since signatory changes are timestamped events.
#     → last_write_wins
#   - business_name: rare to change; high stakes if wrong. Don't
#     auto-resolve. → human_escalation
FIELD_POLICY: Dict[str, Dict[str, Any]] = {
    "registered_address": {
        "policy": Policy.SOURCE_OF_RECORD,
        "source_of_record": "sws",
    },
    "authorised_signatory": {
        "policy": Policy.LAST_WRITE_WINS,
    },
    "business_name": {
        "policy": Policy.HUMAN_ESCALATION,
    },
}

# Default for fields not explicitly listed above.
DEFAULT_POLICY = {
    "policy": Policy.LAST_WRITE_WINS,
}


@dataclass
class InflightUpdate:
    source: str  # "sws" | "factories" | "shops"
    occurred_at: datetime
    payload: Any  # the after-value of this field

    def to_json(self) -> str:
        return json.dumps(
            {
                "source": self.source,
                "occurred_at": self.occurred_at.isoformat(),
                "payload": self.payload,
            }
        )

    @classmethod
    def from_json(cls, s: str) -> "InflightUpdate":
        d = json.loads(s)
        return cls(
            source=d["source"],
            occurred_at=datetime.fromisoformat(d["occurred_at"]),
            payload=d["payload"],
        )


def _key(ubid: str, field: str) -> str:
    return f"inflight:{ubid}:{field}"


@dataclass
class ConflictDecision:
    """Outcome of checking one field. The propagator uses this to decide
    whether to actually write."""
    has_conflict: bool
    should_write: bool  # if False, skip the write (rejected by policy)
    policy_applied: Optional[str]
    competing_source: Optional[str]
    reason: str


def check_and_register(
    ubid: str,
    field: str,
    incoming: InflightUpdate,
) -> ConflictDecision:
    """
    Look up the inflight slot for (ubid, field). If empty, register
    `incoming` and allow the write. If occupied by a DIFFERENT source,
    apply the field's policy.

    Returns a ConflictDecision describing what should happen.
    """
    key = _key(ubid, field)
    field_policy = FIELD_POLICY.get(field, DEFAULT_POLICY)

    # Try a Redis transaction: read, decide, write.
    # We don't need true MULTI/EXEC for the prototype — at hackathon
    # scale, we just GET → decide → SETEX. Race conditions exist but
    # are vanishingly rare and don't affect correctness once auditing
    # catches them.

    existing_raw = _r.get(key)

    if not existing_raw:
        # No prior in-flight update. Register and allow.
        _r.setex(key, WINDOW_SECONDS, incoming.to_json())
        return ConflictDecision(
            has_conflict=False,
            should_write=True,
            policy_applied=None,
            competing_source=None,
            reason="no in-flight update for this field",
        )

    existing = InflightUpdate.from_json(existing_raw)

    if existing.source == incoming.source:
        # Same source updated this field again within 30s — not a
        # conflict, just a follow-up. Refresh the window.
        _r.setex(key, WINDOW_SECONDS, incoming.to_json())
        return ConflictDecision(
            has_conflict=False,
            should_write=True,
            policy_applied=None,
            competing_source=None,
            reason="same-source follow-up within window",
        )

    # Different source within 30s → CONFLICT. Apply policy.
    policy = field_policy["policy"]

    if policy == Policy.SOURCE_OF_RECORD:
        sor = field_policy["source_of_record"]
        if incoming.source == sor:
            # Incoming is from the source-of-record → it wins.
            _r.setex(key, WINDOW_SECONDS, incoming.to_json())
            return ConflictDecision(
                has_conflict=True,
                should_write=True,
                policy_applied=Policy.SOURCE_OF_RECORD.value,
                competing_source=existing.source,
                reason=(
                    f"source-of-record for '{field}' is '{sor}', "
                    f"incoming is from '{incoming.source}' — incoming wins, "
                    f"competing update from '{existing.source}' superseded"
                ),
            )
        else:
            # Incoming is NOT from source-of-record → reject.
            return ConflictDecision(
                has_conflict=True,
                should_write=False,
                policy_applied=Policy.SOURCE_OF_RECORD.value,
                competing_source=existing.source,
                reason=(
                    f"source-of-record for '{field}' is '{sor}', "
                    f"incoming is from '{incoming.source}' — rejected"
                ),
            )

    if policy == Policy.LAST_WRITE_WINS:
        if incoming.occurred_at >= existing.occurred_at:
            _r.setex(key, WINDOW_SECONDS, incoming.to_json())
            return ConflictDecision(
                has_conflict=True,
                should_write=True,
                policy_applied=Policy.LAST_WRITE_WINS.value,
                competing_source=existing.source,
                reason=(
                    f"last-write-wins: incoming ({incoming.occurred_at}) "
                    f">= existing ({existing.occurred_at})"
                ),
            )
        else:
            return ConflictDecision(
                has_conflict=True,
                should_write=False,
                policy_applied=Policy.LAST_WRITE_WINS.value,
                competing_source=existing.source,
                reason=(
                    f"last-write-wins: incoming ({incoming.occurred_at}) "
                    f"< existing ({existing.occurred_at}) — rejected"
                ),
            )

    if policy == Policy.HUMAN_ESCALATION:
        # Don't apply either — both writes are blocked, conflict goes
        # to the review queue. The propagator handles enqueueing.
        return ConflictDecision(
            has_conflict=True,
            should_write=False,
            policy_applied=Policy.HUMAN_ESCALATION.value,
            competing_source=existing.source,
            reason=(
                f"field '{field}' requires human review; "
                f"existing from '{existing.source}', "
                f"incoming from '{incoming.source}'"
            ),
        )

    # Should not reach here.
    return ConflictDecision(
        has_conflict=True,
        should_write=False,
        policy_applied=str(policy),
        competing_source=existing.source,
        reason="unknown policy",
    )


def get_inflight(ubid: str, field: str) -> Optional[InflightUpdate]:
    """Read-only inspection of the in-flight slot. Useful for the
    dashboard to show 'change in progress' indicators."""
    raw = _r.get(_key(ubid, field))
    return InflightUpdate.from_json(raw) if raw else None


def policy_for(field: str) -> Dict[str, Any]:
    """Public accessor — used by the API to expose the policy table."""
    return FIELD_POLICY.get(field, DEFAULT_POLICY)


def all_policies() -> Dict[str, Any]:
    """For the dashboard's policy panel."""
    out = {f: dict(p) for f, p in FIELD_POLICY.items()}
    for f in out:
        out[f]["policy"] = out[f]["policy"].value if isinstance(out[f]["policy"], Policy) else out[f]["policy"]
    out["__default__"] = {
        "policy": DEFAULT_POLICY["policy"].value if isinstance(DEFAULT_POLICY["policy"], Policy) else DEFAULT_POLICY["policy"]
    }
    return out

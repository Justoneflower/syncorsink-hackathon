"""
Propagation orchestrator (v2).

Differences from v1:
- Wraps every per-field propagation in a conflict check (conflict.py)
- Conflict outcomes drive whether the write happens, are audited, and
  human-escalation cases enqueue a ConflictReview row
- Adds reverse-direction propagation (Department → SWS) via
  propagate_dept_event(), used by both the CDC poller and the snapshot
  poller.
"""
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict

import httpx

from . import routing
from .adapters.factories import FactoriesAdapter
from .adapters.shops import ShopsAdapter
from .audit import write_audit
from .conflict import (
    InflightUpdate,
    Policy,
    check_and_register,
    get_inflight,
)
from .db import ConflictReview, SessionLocal

log = logging.getLogger(__name__)

ADAPTERS = {
    "factories": FactoriesAdapter(),
    "shops": ShopsAdapter(),
}

SWS_URL = os.environ.get("SWS_URL", "http://sws-mock:8001")


def _idempotency_key(event_id: str, target: str) -> str:
    h = hashlib.sha256()
    h.update(f"{event_id}::{target}".encode())
    return h.hexdigest()


def _enqueue_for_review(event_id, ubid, field, incoming_source, incoming_value, competing_source, competing_value):
    with SessionLocal() as db:
        db.add(
            ConflictReview(
                event_id=event_id,
                ubid=ubid,
                field=field,
                incoming_source=incoming_source,
                incoming_value=incoming_value,
                competing_source=competing_source,
                competing_value=competing_value,
                status="pending",
            )
        )
        db.commit()


def _check_conflicts(event_id, ubid, source, changed, occurred_at):
    out = {}
    for field, change in changed.items():
        after = change.get("after") if isinstance(change, dict) else change
        incoming = InflightUpdate(source=source, occurred_at=occurred_at, payload=after)
        decision = check_and_register(ubid, field, incoming)

        if decision.has_conflict:
            write_audit(
                event_id=event_id,
                action="conflict",
                ubid=ubid,
                source=source,
                target=None,
                status="ok",
                payload={
                    "field": field,
                    "competing_source": decision.competing_source,
                    "policy": decision.policy_applied,
                    "should_write": decision.should_write,
                    "reason": decision.reason,
                },
            )
            write_audit(
                event_id=event_id,
                action="policy",
                ubid=ubid,
                source=source,
                target=None,
                status="ok" if decision.should_write else "skipped",
                payload={
                    "field": field,
                    "policy": decision.policy_applied,
                    "outcome": "incoming_wins" if decision.should_write else "incoming_rejected",
                },
            )

            if decision.policy_applied == Policy.HUMAN_ESCALATION.value:
                inflight = get_inflight(ubid, field)
                competing = inflight.payload if inflight else None
                _enqueue_for_review(
                    event_id=event_id,
                    ubid=ubid,
                    field=field,
                    incoming_source=source,
                    incoming_value=after,
                    competing_source=decision.competing_source,
                    competing_value=competing,
                )
                write_audit(
                    event_id=event_id,
                    action="review_queued",
                    ubid=ubid,
                    source=source,
                    status="ok",
                    payload={"field": field},
                )

        out[field] = {"should_write": decision.should_write, "decision": decision}
    return out


def _filter_writeable(changed, decisions):
    return {f: c for f, c in changed.items() if decisions[f]["should_write"]}


# Direction 1: SWS → depts
def propagate_sws_event(event: Dict[str, Any]) -> Dict[str, Any]:
    event_id = event.get("event_id") or hashlib.sha256(
        json.dumps(event, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    ubid = event["ubid"]
    pan = event["pan"]
    changed = event.get("changed_fields", {})
    occurred_at = _parse_dt(event.get("occurred_at"))

    write_audit(
        event_id=event_id, action="ingest", ubid=ubid, source="sws",
        status="ok", payload={"changed_field_count": len(changed)},
    )

    decisions = _check_conflicts(event_id, ubid, "sws", changed, occurred_at)
    writeable = _filter_writeable(changed, decisions)

    if not writeable:
        write_audit(
            event_id=event_id, action="propagate", ubid=ubid, source="sws",
            status="skipped", payload={"reason": "all fields rejected by conflict policy"},
        )
        return {"event_id": event_id, "ubid": ubid, "results": [], "decisions": _decisions_summary(decisions)}

    routes = routing.lookup(ubid)
    write_audit(
        event_id=event_id, action="lookup", ubid=ubid, source="sws",
        status="ok", payload={"depts": [r.dept for r in routes]},
    )

    if not routes:
        write_audit(
            event_id=event_id, action="lookup", ubid=ubid, source="sws",
            status="skipped", error="no routing entries (v2: pre-match hold queue)",
        )
        return {"event_id": event_id, "ubid": ubid, "results": [], "decisions": _decisions_summary(decisions)}

    results = []
    for route in routes:
        adapter = ADAPTERS.get(route.dept)
        if not adapter:
            results.append({"dept": route.dept, "status": "no_adapter"})
            continue

        dept_payload: Dict[str, Any] = {}
        for field, change in writeable.items():
            after = change.get("after") if isinstance(change, dict) else change
            if field == "registered_address":
                dept_payload.update(adapter.translate_address_change(after))
            elif field == "authorised_signatory":
                dept_payload.update(adapter.translate_signatory_change(after))

        if not dept_payload:
            continue

        write_audit(
            event_id=event_id, action="translate", ubid=ubid, source="sws",
            target=route.dept, status="ok", payload=dept_payload,
        )

        attempts, last_error, ok = 0, None, False
        while attempts < 2:
            attempts += 1
            try:
                adapter.write(pan, dept_payload)
                write_audit(
                    event_id=event_id, action="write", ubid=ubid, source="sws",
                    target=route.dept, status="ok",
                    payload={"attempt": attempts, "idempotency_key": _idempotency_key(event_id, route.dept)},
                )
                results.append({"dept": route.dept, "status": "ok", "attempts": attempts})
                ok = True
                break
            except Exception as e:
                last_error = str(e)
                if attempts < 2:
                    time.sleep(0.3)
        if not ok:
            write_audit(
                event_id=event_id, action="write", ubid=ubid, source="sws",
                target=route.dept, status="error", error=last_error,
                payload={"attempts": attempts},
            )
            results.append({"dept": route.dept, "status": "error", "attempts": attempts, "error": last_error})

    return {"event_id": event_id, "ubid": ubid, "results": results, "decisions": _decisions_summary(decisions)}


# Direction 2: dept → SWS
def propagate_dept_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """`changed_fields` is already in canonical SWS shape — the pollers
    do reverse translation before calling us."""
    source = event["source"]
    event_id = event.get("event_id") or hashlib.sha256(
        json.dumps(event, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    ubid = event["ubid"]
    changed = event.get("changed_fields", {})
    occurred_at = _parse_dt(event.get("occurred_at"))

    write_audit(
        event_id=event_id, action="ingest", ubid=ubid, source=source,
        status="ok", payload={"changed_field_count": len(changed)},
    )

    decisions = _check_conflicts(event_id, ubid, source, changed, occurred_at)
    writeable = _filter_writeable(changed, decisions)

    if not writeable:
        write_audit(
            event_id=event_id, action="propagate", ubid=ubid, source=source,
            status="skipped", payload={"reason": "all fields rejected by conflict policy"},
        )
        return {"event_id": event_id, "ubid": ubid, "wrote_to_sws": False, "decisions": _decisions_summary(decisions)}

    sws_patch: Dict[str, Any] = {}
    for field, change in writeable.items():
        after = change.get("after") if isinstance(change, dict) else change
        if field in ("registered_address", "authorised_signatory", "business_name"):
            sws_patch[field] = after

    if not sws_patch:
        return {"event_id": event_id, "ubid": ubid, "wrote_to_sws": False, "decisions": _decisions_summary(decisions)}

    write_audit(
        event_id=event_id, action="translate", ubid=ubid, source=source,
        target="sws", status="ok", payload=sws_patch,
    )

    attempts, last_error, ok = 0, None, False
    while attempts < 2:
        attempts += 1
        try:
            with httpx.Client(timeout=5.0) as client:
                # X-SyncOrSink-Origin tells SWS-mock to suppress its
                # outbound webhook so we don't loop.
                r = client.patch(
                    f"{SWS_URL}/businesses/{ubid}",
                    json=sws_patch,
                    headers={"X-SyncOrSink-Origin": source},
                )
                r.raise_for_status()
            write_audit(
                event_id=event_id, action="write", ubid=ubid, source=source,
                target="sws", status="ok",
                payload={"attempt": attempts, "idempotency_key": _idempotency_key(event_id, "sws")},
            )
            ok = True
            break
        except Exception as e:
            last_error = str(e)
            if attempts < 2:
                time.sleep(0.3)
    if not ok:
        write_audit(
            event_id=event_id, action="write", ubid=ubid, source=source,
            target="sws", status="error", error=last_error,
            payload={"attempts": attempts},
        )

    return {"event_id": event_id, "ubid": ubid, "wrote_to_sws": ok, "decisions": _decisions_summary(decisions)}


def _parse_dt(s: Any) -> datetime:
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    if isinstance(s, str):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _decisions_summary(decisions):
    out = {}
    for field, info in decisions.items():
        d = info["decision"]
        out[field] = {
            "has_conflict": d.has_conflict,
            "should_write": d.should_write,
            "policy_applied": d.policy_applied,
            "competing_source": d.competing_source,
            "reason": d.reason,
        }
    return out

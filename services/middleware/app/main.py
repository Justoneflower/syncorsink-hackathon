"""
SyncOrSink middleware (v2).

Endpoints:
  POST /webhooks/sws            Direction 1 ingestion (called by SWS mock)
  GET  /trace/{ubid}            End-to-end audit trace for a UBID
  GET  /audit                   Recent audit entries (powers dashboard feed)
  GET  /routing                 Current routing index
  GET  /conflicts               Pending conflict reviews (human-escalation)
  POST /conflicts/{id}/resolve  Officer resolves a conflict
  GET  /policies                Current per-field conflict policies
  GET  /businesses              All UBIDs known to routing
  GET  /health                  Liveness

Background:
  Two pollers (factories, shops) running as asyncio tasks, scanning
  dept mocks every 3s for changes and feeding propagate_dept_event.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import conflict, routing as routing_mod
from .db import AuditEntry, ConflictReview, SessionLocal, init_schema
from .pollers import start_pollers
from .propagator import propagate_sws_event

logging.basicConfig(level=logging.INFO, format="%(asctime)s [MIDDLEWARE] %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="SyncOrSink Middleware (v2)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    init_schema()
    log.info("middleware schema ready")
    # Background pollers — don't await; they run forever.
    start_pollers(asyncio.get_event_loop())
    log.info("pollers scheduled (factories + shops, 3s interval)")


@app.get("/health")
def health():
    return {"status": "ok", "service": "middleware"}


# --- Direction 1 ingestion -------------------------------------------------

@app.post("/webhooks/sws")
def sws_webhook(event: dict):
    log.info(
        f"received SWS event ubid={event.get('ubid')} "
        f"fields={list(event.get('changed_fields', {}).keys())}"
    )
    summary = propagate_sws_event(event)
    log.info(
        f"propagation summary ubid={summary['ubid']} "
        f"results={[(r['dept'], r['status']) for r in summary['results']]}"
    )
    return summary


# --- Audit & trace ---------------------------------------------------------

@app.get("/trace/{ubid}")
def trace(ubid: str, limit: int = 200):
    with SessionLocal() as db:
        rows = (
            db.query(AuditEntry)
            .filter(AuditEntry.ubid == ubid)
            .order_by(AuditEntry.occurred_at.asc())
            .limit(limit)
            .all()
        )
        return [
            {
                "occurred_at": r.occurred_at.isoformat(),
                "event_id": r.event_id,
                "source": r.source,
                "target": r.target,
                "action": r.action,
                "status": r.status,
                "payload": r.payload,
                "error": r.error,
            }
            for r in rows
        ]


@app.get("/audit")
def audit_feed(limit: int = 50):
    with SessionLocal() as db:
        rows = (
            db.query(AuditEntry)
            .order_by(AuditEntry.occurred_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "occurred_at": r.occurred_at.isoformat(),
                "event_id": r.event_id,
                "ubid": r.ubid,
                "source": r.source,
                "target": r.target,
                "action": r.action,
                "status": r.status,
            }
            for r in rows
        ]


# --- Routing ---------------------------------------------------------------

@app.get("/routing")
def routing_dump():
    return [
        {
            "ubid": r.ubid,
            "dept": r.dept,
            "dept_native_id": r.dept_native_id,
            "pan": r.pan,
            "confidence": r.confidence,
        }
        for r in routing_mod.all_entries()
    ]


@app.get("/businesses")
def list_businesses():
    """Distinct UBIDs in routing — used by the dashboard's selector."""
    seen = {}
    for entry in routing_mod.all_entries():
        seen.setdefault(entry.ubid, {"ubid": entry.ubid, "pan": entry.pan, "depts": []})
        seen[entry.ubid]["depts"].append(entry.dept)
    return list(seen.values())


# --- Conflict review queue -------------------------------------------------

class ResolveRequest(BaseModel):
    resolution: str  # "incoming" | "competing" | "manual"
    resolved_by: str = "officer"
    manual_value: Optional[dict] = None


@app.get("/conflicts")
def list_conflicts(status: str = "pending", limit: int = 50):
    with SessionLocal() as db:
        q = db.query(ConflictReview)
        if status != "all":
            q = q.filter(ConflictReview.status == status)
        rows = q.order_by(ConflictReview.created_at.desc()).limit(limit).all()
        return [
            {
                "id": r.id,
                "event_id": r.event_id,
                "ubid": r.ubid,
                "field": r.field,
                "incoming_source": r.incoming_source,
                "incoming_value": r.incoming_value,
                "competing_source": r.competing_source,
                "competing_value": r.competing_value,
                "status": r.status,
                "resolution": r.resolution,
                "resolved_by": r.resolved_by,
                "created_at": r.created_at.isoformat(),
                "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
            }
            for r in rows
        ]


@app.post("/conflicts/{conflict_id}/resolve")
def resolve_conflict(conflict_id: int, body: ResolveRequest):
    """Officer picks a winner. We update the row but don't (yet) re-fire
    the propagation — that's a v3 enhancement (for the demo, the
    conflict-resolution audit entry is enough to show the workflow)."""
    with SessionLocal() as db:
        c = db.get(ConflictReview, conflict_id)
        if not c:
            raise HTTPException(404, "conflict not found")
        if c.status != "pending":
            raise HTTPException(400, f"conflict already {c.status}")
        c.status = "resolved"
        c.resolution = body.resolution
        c.resolved_by = body.resolved_by
        c.resolved_at = datetime.now(timezone.utc)
        db.commit()

        # Audit the manual resolution
        from .audit import write_audit
        write_audit(
            event_id=c.event_id,
            action="manual_resolve",
            ubid=c.ubid,
            source=c.incoming_source,
            status="ok",
            payload={
                "field": c.field,
                "resolution": body.resolution,
                "resolved_by": body.resolved_by,
            },
        )
        return {"status": "ok", "conflict_id": conflict_id, "resolution": body.resolution}


# --- Policies (read-only for the dashboard) --------------------------------

@app.get("/policies")
def list_policies():
    return conflict.all_policies()

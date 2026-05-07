"""
SWS Mock — simulates Karnataka SWS (sws.investkarnataka.co.in).

This is the modern, forward-looking system. Schema is structured JSON.
Emits an outbound webhook on any business mutation, which the middleware
consumes to propagate changes to legacy department systems.

NOTE: schema is plausible/representative — real SWS API is auth-walled
and not publicly documented.
"""
import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, String, JSON, DateTime, text
from sqlalchemy.orm import declarative_base, sessionmaker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [SWS] %(message)s")
log = logging.getLogger(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]
WEBHOOK_URL = os.environ["MIDDLEWARE_WEBHOOK_URL"]

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False)
Base = declarative_base()


class Business(Base):
    __tablename__ = "businesses"
    ubid = Column(String, primary_key=True)
    pan = Column(String, nullable=False, index=True)
    cin = Column(String)
    business_name = Column(String, nullable=False)
    registered_address = Column(JSON, nullable=False)
    authorised_signatory = Column(JSON, nullable=False)
    directors = Column(JSON, default=list)
    last_updated = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Address(BaseModel):
    line1: str
    line2: Optional[str] = ""
    city: str
    district: str
    state: str = "Karnataka"
    pincode: str


class Signatory(BaseModel):
    name: str
    designation: str
    din: Optional[str] = None


class BusinessOut(BaseModel):
    ubid: str
    pan: str
    cin: Optional[str] = None
    business_name: str
    registered_address: Address
    authorised_signatory: Signatory
    directors: list = Field(default_factory=list)
    last_updated: datetime


class BusinessPatch(BaseModel):
    registered_address: Optional[Address] = None
    authorised_signatory: Optional[Signatory] = None
    business_name: Optional[str] = None


app = FastAPI(title="SWS Mock — Karnataka Single Window System")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(engine)
    log.info("SWS schema ready")


@app.get("/health")
def health():
    return {"status": "ok", "service": "sws-mock"}


@app.get("/businesses/{ubid}", response_model=BusinessOut)
def get_business(ubid: str):
    with SessionLocal() as db:
        b = db.get(Business, ubid)
        if not b:
            raise HTTPException(404, f"Business {ubid} not found")
        return BusinessOut(
            ubid=b.ubid,
            pan=b.pan,
            cin=b.cin,
            business_name=b.business_name,
            registered_address=Address(**b.registered_address),
            authorised_signatory=Signatory(**b.authorised_signatory),
            directors=b.directors or [],
            last_updated=b.last_updated,
        )


@app.get("/businesses", response_model=list[BusinessOut])
def list_businesses():
    with SessionLocal() as db:
        rows = db.query(Business).all()
        return [
            BusinessOut(
                ubid=b.ubid,
                pan=b.pan,
                cin=b.cin,
                business_name=b.business_name,
                registered_address=Address(**b.registered_address),
                authorised_signatory=Signatory(**b.authorised_signatory),
                directors=b.directors or [],
                last_updated=b.last_updated,
            )
            for b in rows
        ]


@app.put("/businesses/{ubid}", response_model=BusinessOut)
def upsert_business(ubid: str, payload: dict):
    """Used by seed and by middleware (Direction 2 writes from depts)."""
    with SessionLocal() as db:
        b = db.get(Business, ubid)
        is_new = b is None
        if is_new:
            b = Business(ubid=ubid)
            db.add(b)
        b.pan = payload["pan"]
        b.cin = payload.get("cin")
        b.business_name = payload["business_name"]
        b.registered_address = payload["registered_address"]
        b.authorised_signatory = payload["authorised_signatory"]
        b.directors = payload.get("directors", [])
        b.last_updated = datetime.now(timezone.utc)
        db.commit()
        db.refresh(b)
        # Don't fire webhook on initial seed — only on user-driven mutations
        # (PATCH below). PUT is treated as an admin upsert.
        return BusinessOut(
            ubid=b.ubid,
            pan=b.pan,
            cin=b.cin,
            business_name=b.business_name,
            registered_address=Address(**b.registered_address),
            authorised_signatory=Signatory(**b.authorised_signatory),
            directors=b.directors or [],
            last_updated=b.last_updated,
        )


@app.patch("/businesses/{ubid}", response_model=BusinessOut)
def patch_business(ubid: str, patch: BusinessPatch, request: Request):
    """User-facing change. Triggers outbound webhook to middleware,
    UNLESS the request originated from the middleware itself (loop guard)."""
    suppress_webhook = bool(request.headers.get("X-SyncOrSink-Origin"))
    with SessionLocal() as db:
        b = db.get(Business, ubid)
        if not b:
            raise HTTPException(404, f"Business {ubid} not found")

        changed_fields = {}
        if patch.registered_address is not None:
            changed_fields["registered_address"] = {
                "before": b.registered_address,
                "after": patch.registered_address.model_dump(),
            }
            b.registered_address = patch.registered_address.model_dump()
        if patch.authorised_signatory is not None:
            changed_fields["authorised_signatory"] = {
                "before": b.authorised_signatory,
                "after": patch.authorised_signatory.model_dump(),
            }
            b.authorised_signatory = patch.authorised_signatory.model_dump()
        if patch.business_name is not None:
            changed_fields["business_name"] = {
                "before": b.business_name,
                "after": patch.business_name,
            }
            b.business_name = patch.business_name

        b.last_updated = datetime.now(timezone.utc)
        db.commit()
        db.refresh(b)

        # Fire webhook unless this PATCH originated from the middleware
        # (Direction 2 loop guard). Best-effort delivery; in production
        # this would be an outbox + worker.
        if changed_fields and not suppress_webhook:
            event = {
                "event_type": "business.updated",
                "source": "sws",
                "ubid": ubid,
                "pan": b.pan,
                "changed_fields": changed_fields,
                "occurred_at": b.last_updated.isoformat(),
            }
            try:
                with httpx.Client(timeout=5.0) as client:
                    r = client.post(WEBHOOK_URL, json=event)
                    log.info(f"webhook → middleware: {r.status_code} for {ubid}")
            except Exception as e:
                log.warning(f"webhook delivery failed: {e}")

        return BusinessOut(
            ubid=b.ubid,
            pan=b.pan,
            cin=b.cin,
            business_name=b.business_name,
            registered_address=Address(**b.registered_address),
            authorised_signatory=Signatory(**b.authorised_signatory),
            directors=b.directors or [],
            last_updated=b.last_updated,
        )

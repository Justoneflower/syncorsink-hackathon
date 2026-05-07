"""
Shops Mock — simulates e-Karmika (ekarmika.karnataka.gov.in).

Karnataka Shops & Commercial Establishments registration system, run by
the Labour Department. Yet another schema variant:

- Address is SEMI-STRUCTURED: {door_no, street, locality, city, pin}
  (not flat string like Factories, not the rich SWS structure)
- Field is `pan_no` (with underscore), not `pan` or `biz_pan`
- Field is `employer_name`, not `occupier_name` or `authorised_signatory`
- Primary key is `registration_no` (e.g. "BNG-31/SHP/CE/2020/55821")

NO webhooks, NO change-feed API. Middleware will poll /changes_since in v2
to discover updates. This is the "snapshot polling fallback" case.
"""
import os
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Integer, JSON, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [SHOPS] %(message)s")
log = logging.getLogger(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False)
Base = declarative_base()


class Establishment(Base):
    __tablename__ = "establishments"
    registration_no = Column(String, primary_key=True)
    establishment_name = Column(String, nullable=False)
    employer_name = Column(String, nullable=False)
    pan_no = Column(String, nullable=False, index=True)
    address_of_establishment = Column(JSON, nullable=False)  # semi-structured dict
    no_of_employees = Column(Integer, default=0)
    category = Column(String, default="Commercial Establishment")
    valid_till = Column(String)
    renewal_status = Column(String, default="ACTIVE")
    last_changed = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)


class AddressSemi(BaseModel):
    door_no: str
    street: str
    locality: str
    city: str
    pin: str


class EstablishmentOut(BaseModel):
    registration_no: str
    establishment_name: str
    employer_name: str
    pan_no: str
    address_of_establishment: AddressSemi
    no_of_employees: int = 0
    category: str = "Commercial Establishment"
    valid_till: Optional[str] = None
    renewal_status: str = "ACTIVE"
    last_changed: datetime


class EstablishmentUpsert(BaseModel):
    registration_no: str
    establishment_name: str
    employer_name: str
    pan_no: str
    address_of_establishment: AddressSemi
    no_of_employees: int = 0
    category: str = "Commercial Establishment"
    valid_till: Optional[str] = None
    renewal_status: str = "ACTIVE"


class EstablishmentPatch(BaseModel):
    address_of_establishment: Optional[AddressSemi] = None
    employer_name: Optional[str] = None
    establishment_name: Optional[str] = None
    no_of_employees: Optional[int] = None
    renewal_status: Optional[str] = None


app = FastAPI(title="Shops Mock — e-Karmika")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(engine)
    log.info("Shops schema ready")


def _to_out(e: Establishment) -> EstablishmentOut:
    return EstablishmentOut(
        registration_no=e.registration_no,
        establishment_name=e.establishment_name,
        employer_name=e.employer_name,
        pan_no=e.pan_no,
        address_of_establishment=AddressSemi(**e.address_of_establishment),
        no_of_employees=e.no_of_employees,
        category=e.category,
        valid_till=e.valid_till,
        renewal_status=e.renewal_status,
        last_changed=e.last_changed,
    )


@app.get("/health")
def health():
    return {"status": "ok", "service": "shops-mock"}


# IMPORTANT: specific routes before catch-all path params (same FastAPI
# routing-order rule as factories-mock).

@app.get("/establishments", response_model=list[EstablishmentOut])
def list_establishments():
    with SessionLocal() as db:
        rows = db.query(Establishment).all()
        return [_to_out(e) for e in rows]


@app.get("/changes_since", response_model=list[EstablishmentOut])
def changes_since(since: datetime = Query(..., description="ISO timestamp")):
    """The poll endpoint. Middleware will hit this every N seconds in v2
    to discover changes (since this dept emits no events)."""
    with SessionLocal() as db:
        rows = (
            db.query(Establishment)
            .filter(Establishment.last_changed > since)
            .order_by(Establishment.last_changed.asc())
            .all()
        )
        return [_to_out(e) for e in rows]


@app.get("/establishments/by-pan/{pan}", response_model=EstablishmentOut)
def get_establishment_by_pan(pan: str):
    with SessionLocal() as db:
        e = db.query(Establishment).filter(Establishment.pan_no == pan).first()
        if not e:
            raise HTTPException(404, f"No establishment for PAN {pan}")
        return _to_out(e)


@app.get("/establishments/{reg_no:path}", response_model=EstablishmentOut)
def get_establishment(reg_no: str):
    with SessionLocal() as db:
        e = db.get(Establishment, reg_no)
        if not e:
            raise HTTPException(404, f"Establishment {reg_no} not found")
        return _to_out(e)


@app.put("/establishments", response_model=EstablishmentOut)
def upsert_establishment(payload: EstablishmentUpsert):
    with SessionLocal() as db:
        e = db.get(Establishment, payload.registration_no)
        if e is None:
            e = Establishment(registration_no=payload.registration_no)
            db.add(e)
        d = payload.model_dump()
        d["address_of_establishment"] = payload.address_of_establishment.model_dump()
        for k, v in d.items():
            setattr(e, k, v)
        e.last_changed = datetime.now(timezone.utc)
        db.commit()
        db.refresh(e)
        return _to_out(e)


@app.patch("/establishments/by-pan/{pan}", response_model=EstablishmentOut)
def patch_establishment_by_pan(pan: str, patch: EstablishmentPatch):
    with SessionLocal() as db:
        e = db.query(Establishment).filter(Establishment.pan_no == pan).first()
        if not e:
            raise HTTPException(404, f"No establishment for PAN {pan}")
        d = patch.model_dump(exclude_unset=True)
        if "address_of_establishment" in d and d["address_of_establishment"] is not None:
            d["address_of_establishment"] = patch.address_of_establishment.model_dump()
        for k, v in d.items():
            setattr(e, k, v)
        e.last_changed = datetime.now(timezone.utc)
        db.commit()
        db.refresh(e)
        log.info(
            f"establishment updated: reg={e.registration_no} "
            f"changed={list(patch.model_dump(exclude_unset=True).keys())}"
        )
        return _to_out(e)

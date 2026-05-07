"""
Factories Mock — simulates FBIS / esuraksha.karnataka.gov.in.

Department of Factories, Boilers, Industrial Safety & Health.
Legacy NIC-style stack. Note the deliberately different schema:

- Address is a SINGLE STRING (not structured), often uppercase
- Field is `biz_pan` not `pan`
- Field is `occupier_name` not `authorised_signatory.name`
- Primary key is `factory_license_no` (e.g. "KA/FAC/BLR-U/2019/14782") NOT UBID
- NO outbound events. Middleware will use Postgres CDC in v2.

This represents real schema heterogeneity: every legacy dept has its own
field names and conventions. The middleware's translation layer must handle
this without modifying the source system.
"""
import os
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Integer, Float, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [FACTORIES] %(message)s")
log = logging.getLogger(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False)
Base = declarative_base()


class Factory(Base):
    """Factory licensing record. Note the legacy field names."""
    __tablename__ = "factories"
    factory_license_no = Column(String, primary_key=True)
    biz_pan = Column(String, nullable=False, index=True)
    factory_name = Column(String, nullable=False)
    premises_address = Column(String, nullable=False)  # SINGLE STRING — like real esuraksha forms
    occupier_name = Column(String, nullable=False)
    manager_name = Column(String)
    license_validity = Column(String)  # ISO date string
    workers_count = Column(Integer, default=0)
    power_kw = Column(Float, default=0.0)
    license_status = Column(String, default="ACTIVE")
    last_modified = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class FactoryOut(BaseModel):
    factory_license_no: str
    biz_pan: str
    factory_name: str
    premises_address: str
    occupier_name: str
    manager_name: Optional[str] = None
    license_validity: Optional[str] = None
    workers_count: int = 0
    power_kw: float = 0.0
    license_status: str = "ACTIVE"
    last_modified: datetime


class FactoryUpsert(BaseModel):
    factory_license_no: str
    biz_pan: str
    factory_name: str
    premises_address: str
    occupier_name: str
    manager_name: Optional[str] = None
    license_validity: Optional[str] = None
    workers_count: int = 0
    power_kw: float = 0.0
    license_status: str = "ACTIVE"


class FactoryPatch(BaseModel):
    """Whatever the middleware (or an officer) wants to change."""
    premises_address: Optional[str] = None
    occupier_name: Optional[str] = None
    manager_name: Optional[str] = None
    factory_name: Optional[str] = None
    workers_count: Optional[int] = None
    power_kw: Optional[float] = None
    license_status: Optional[str] = None


app = FastAPI(title="Factories Mock — FBIS / esuraksha")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(engine)
    log.info("Factories schema ready")


@app.get("/health")
def health():
    return {"status": "ok", "service": "factories-mock"}


# IMPORTANT: route order matters. Specific paths MUST come before
# catch-all path parameters, because FastAPI matches in declaration order.
# `{license_no:path}` is greedy and would otherwise shadow `/by-pan/{pan}`.

@app.get("/factories", response_model=list[FactoryOut])
def list_factories():
    with SessionLocal() as db:
        rows = db.query(Factory).all()
        return [
            FactoryOut(**{c.name: getattr(f, c.name) for c in f.__table__.columns})
            for f in rows
        ]


@app.get("/factories/by-pan/{pan}", response_model=FactoryOut)
def get_factory_by_pan(pan: str):
    """Useful for the middleware's UBID→PAN→license_no resolution."""
    with SessionLocal() as db:
        f = db.query(Factory).filter(Factory.biz_pan == pan).first()
        if not f:
            raise HTTPException(404, f"No factory for PAN {pan}")
        return FactoryOut(**{c.name: getattr(f, c.name) for c in f.__table__.columns})


@app.get("/factories/{license_no:path}", response_model=FactoryOut)
def get_factory(license_no: str):
    """Look up by full factory license number (which may contain slashes)."""
    with SessionLocal() as db:
        f = db.get(Factory, license_no)
        if not f:
            raise HTTPException(404, f"Factory {license_no} not found")
        return FactoryOut(**{c.name: getattr(f, c.name) for c in f.__table__.columns})


@app.put("/factories", response_model=FactoryOut)
def upsert_factory(payload: FactoryUpsert):
    """Used for seed and middleware-driven creates."""
    with SessionLocal() as db:
        f = db.get(Factory, payload.factory_license_no)
        if f is None:
            f = Factory(factory_license_no=payload.factory_license_no)
            db.add(f)
        for k, v in payload.model_dump().items():
            setattr(f, k, v)
        f.last_modified = datetime.now(timezone.utc)
        db.commit()
        db.refresh(f)
        return FactoryOut(**{c.name: getattr(f, c.name) for c in f.__table__.columns})


@app.patch("/factories/by-pan/{pan}", response_model=FactoryOut)
def patch_factory_by_pan(pan: str, patch: FactoryPatch):
    """Middleware writes here using PAN as join key. In v2, an officer
    changing fields directly on this endpoint would be picked up by CDC
    and propagated back to SWS (Direction 2)."""
    with SessionLocal() as db:
        f = db.query(Factory).filter(Factory.biz_pan == pan).first()
        if not f:
            raise HTTPException(404, f"No factory for PAN {pan}")
        for k, v in patch.model_dump(exclude_unset=True).items():
            setattr(f, k, v)
        f.last_modified = datetime.now(timezone.utc)
        db.commit()
        db.refresh(f)
        log.info(
            f"factory updated: license={f.factory_license_no} "
            f"changed={list(patch.model_dump(exclude_unset=True).keys())}"
        )
        return FactoryOut(**{c.name: getattr(f, c.name) for c in f.__table__.columns})

"""
Seed script. Loads synthetic Karnataka businesses into:
  - SWS mock (canonical structured form)
  - Factories mock (single-string addresses, occupier_name)
  - Shops mock (semi-structured address, employer_name)

Then populates the middleware's routing index so it knows which depts
have records for each UBID. In production this index would be built via
nightly reconciliation + event-driven updates; here we seed it directly.

Run via:
  docker-compose exec middleware python -m app.seed
"""
import json
import os
import sys
import time
from pathlib import Path

import httpx

from .db import init_schema
from . import routing as routing_mod

SWS_URL = os.environ.get("SWS_URL", "http://sws-mock:8001")
FACTORIES_URL = os.environ.get("FACTORIES_URL", "http://factories-mock:8002")
SHOPS_URL = os.environ.get("SHOPS_URL", "http://shops-mock:8003")
SEED_FILE = Path("/app/data/seed.json")


def wait_for(url: str, name: str, timeout: int = 30):
    """Block until a service's /health responds."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{url}/health", timeout=2.0)
            if r.status_code == 200:
                print(f"  ✓ {name} ready")
                return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(f"{name} not ready after {timeout}s")


def seed_sws(business: dict):
    """SWS: PUT the canonical record. PUT does not fire webhooks (admin upsert)."""
    payload = {
        "pan": business["pan"],
        "cin": business["cin"],
        "business_name": business["business_name"],
        "registered_address": business["registered_address"],
        "authorised_signatory": business["authorised_signatory"],
        "directors": business["directors"],
    }
    r = httpx.put(f"{SWS_URL}/businesses/{business['ubid']}", json=payload, timeout=5.0)
    r.raise_for_status()


def _flatten_addr(addr: dict) -> str:
    """SWS structured → Factories single-string. Mirrors FactoriesAdapter."""
    parts = [addr.get("line1", ""), addr.get("line2", ""), addr.get("city", "")]
    s = ", ".join(p.strip() for p in parts if p and p.strip())
    if addr.get("pincode"):
        s = f"{s} - {addr['pincode']}"
    return s.upper()


def _split_addr(addr: dict) -> dict:
    """SWS structured → Shops semi-structured. Mirrors ShopsAdapter."""
    line1 = (addr.get("line1") or "").strip()
    line2 = (addr.get("line2") or "").strip()
    if "," in line1:
        door, _, rest = line1.partition(",")
        door, street = door.strip(), rest.strip()
    else:
        door, street = line1, ""
    if not street and line2:
        street, locality = line2, ""
    else:
        locality = line2
    return {
        "door_no": door,
        "street": street,
        "locality": locality,
        "city": addr.get("city", ""),
        "pin": addr.get("pincode", ""),
    }


def seed_factories(business: dict):
    payload = {
        "factory_license_no": business["factory_license_no"],
        "biz_pan": business["pan"],
        "factory_name": business["business_name"].upper(),
        "premises_address": _flatten_addr(business["registered_address"]),
        "occupier_name": business["authorised_signatory"]["name"].upper(),
        "manager_name": business["factory_manager_name"],
        "license_validity": business["factory_license_validity"],
        "workers_count": business["factory_workers_count"],
        "power_kw": business["factory_power_kw"],
        "license_status": "ACTIVE",
    }
    r = httpx.put(f"{FACTORIES_URL}/factories", json=payload, timeout=5.0)
    r.raise_for_status()


def seed_shops(business: dict):
    payload = {
        "registration_no": business["shop_registration_no"],
        "establishment_name": business["business_name"],
        "employer_name": business["authorised_signatory"]["name"],
        "pan_no": business["pan"],
        "address_of_establishment": _split_addr(business["registered_address"]),
        "no_of_employees": business["shop_employees"],
        "category": "Commercial Establishment",
        "valid_till": business["shop_valid_till"],
        "renewal_status": "ACTIVE",
    }
    r = httpx.put(f"{SHOPS_URL}/establishments", json=payload, timeout=5.0)
    r.raise_for_status()


def main():
    print("Initialising middleware schema...")
    init_schema()

    print("Waiting for services to be ready...")
    wait_for(SWS_URL, "sws-mock")
    wait_for(FACTORIES_URL, "factories-mock")
    wait_for(SHOPS_URL, "shops-mock")

    if not SEED_FILE.exists():
        print(f"FATAL: seed file missing at {SEED_FILE}", file=sys.stderr)
        sys.exit(1)

    businesses = json.loads(SEED_FILE.read_text())

    print(f"\nSeeding {len(businesses)} businesses across SWS + Factories + Shops...")
    for b in businesses:
        seed_sws(b)
        seed_factories(b)
        seed_shops(b)
        # Routing entries
        routing_mod.upsert(b["ubid"], "factories", b["factory_license_no"], b["pan"])
        routing_mod.upsert(b["ubid"], "shops", b["shop_registration_no"], b["pan"])
        print(f"  ✓ {b['ubid']}  ({b['business_name']})")

    print(f"\nSeed complete. Routing index has {len(routing_mod.all_entries())} entries.")
    print("\nTry:")
    print("  curl http://localhost:8001/businesses/KA-UBID-2025-0089123 | jq")
    print("  curl http://localhost:8000/routing | jq")
    print("  ./scripts/demo-scenario-1.sh")


if __name__ == "__main__":
    main()

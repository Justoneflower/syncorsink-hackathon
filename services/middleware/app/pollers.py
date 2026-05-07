"""
Direction 2 change discovery.

The problem statement explicitly calls out: "Some department systems do
not natively emit events, so the layer must be able to pick up changes
from whatever surface is available — API, webhook, polling or snapshot
comparison."

We demonstrate THREE discovery surfaces in v2:

1. Webhook (sws-mock) — already wired in v1.
2. Snapshot polling + delta comparison (shops-mock) — implemented here.
   We hit /changes_since every 3 seconds, compare each row against the
   last-known snapshot kept in middleware Postgres, and emit Direction 2
   events for fields that actually changed.
3. CDC simulation (factories-mock) — implemented here. We poll
   /factories every 3 seconds (in production, Debezium would tail the
   Postgres WAL; same effect, lower latency).

For each detected change, we:
  a) Translate dept-native shape → SWS canonical shape (reverse adapter)
  b) Build a {field: {before, after}} change set
  c) Call propagate_dept_event() — which runs the conflict checks and
     writes to SWS

Pollers run as asyncio background tasks. Each is independent; failure
of one does not affect others.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from .propagator import propagate_dept_event
from . import routing

log = logging.getLogger(__name__)

FACTORIES_URL = os.environ.get("FACTORIES_URL", "http://factories-mock:8002")
SHOPS_URL = os.environ.get("SHOPS_URL", "http://shops-mock:8003")

POLL_INTERVAL_SEC = 3.0

# In-memory last-seen snapshots, keyed by (dept, native_id). Restart-safe
# because on startup we reseed from current dept state and emit no
# spurious events. Production would persist this in Postgres.
_last_snapshot: Dict[str, Dict[str, Any]] = {
    "factories": {},
    "shops": {},
}


# ---------------------------------------------------------------------------
# Reverse translation: dept-native → SWS-canonical
# ---------------------------------------------------------------------------

def _factories_to_canonical(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Factories stores premises_address as a single uppercase string.
    Reverse-translation parses it best-effort. The PERFECT round-trip is
    impossible (the structured form has more info than the flat form
    can carry), so we only emit the fields we can recover confidently.
    """
    addr = (row.get("premises_address") or "").strip()
    parts = [p.strip() for p in addr.split(",")]
    pincode = ""
    line1 = parts[0] if parts else ""
    line2 = parts[1] if len(parts) > 1 else ""
    city = "Bengaluru"

    if " - " in addr:
        head, _, tail = addr.rpartition(" - ")
        pincode = tail.strip()
        # Refresh `parts` based on `head` (without the pincode tail)
        parts = [p.strip() for p in head.split(",")]
        line1 = parts[0] if parts else ""
        line2 = parts[1] if len(parts) > 1 else ""
        city = parts[-1].strip().title() if len(parts) >= 3 else "Bengaluru"

    canonical = {
        "registered_address": {
            "line1": line1.title(),
            "line2": line2.title(),
            "city": city,
            "district": "Bengaluru Urban",
            "state": "Karnataka",
            "pincode": pincode,
        },
        "authorised_signatory": {
            "name": (row.get("occupier_name") or "").title(),
            "designation": "Occupier (Factories Act)",  # best-effort
        },
    }
    return canonical


def _shops_to_canonical(row: Dict[str, Any]) -> Dict[str, Any]:
    addr = row.get("address_of_establishment") or {}
    line1 = addr.get("door_no", "")
    if addr.get("street"):
        line1 = f"{line1}, {addr['street']}".strip(", ")
    return {
        "registered_address": {
            "line1": line1,
            "line2": addr.get("locality", ""),
            "city": addr.get("city", ""),
            "district": "Bengaluru Urban",
            "state": "Karnataka",
            "pincode": addr.get("pin", ""),
        },
        "authorised_signatory": {
            "name": row.get("employer_name", ""),
            "designation": "Employer (Shops Act)",
        },
    }


# ---------------------------------------------------------------------------
# Delta detection
# ---------------------------------------------------------------------------

def _delta(prev: Optional[Dict[str, Any]], curr: Dict[str, Any]) -> Dict[str, Any]:
    """Compare two canonical-shape dicts. Return {field: {before, after}}
    only for fields whose values differ."""
    if prev is None:
        return {}  # first sighting — don't fire spurious events
    out = {}
    for k, v in curr.items():
        if prev.get(k) != v:
            out[k] = {"before": prev.get(k), "after": v}
    return out


def _resolve_ubid_for(dept: str, native_id: str) -> Optional[str]:
    """Look up UBID via routing index (PAN-based join from seed)."""
    for entry in routing.all_entries():
        if entry.dept == dept and entry.dept_native_id == native_id:
            return entry.ubid
    return None


# ---------------------------------------------------------------------------
# Pollers
# ---------------------------------------------------------------------------

async def poll_factories():
    """Simulates Debezium CDC. In production, replace with a Kafka
    consumer reading the Debezium topic. Same logical output."""
    log.info("factories poller starting")
    while True:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{FACTORIES_URL}/factories")
                r.raise_for_status()
                rows = r.json()

            for row in rows:
                native_id = row["factory_license_no"]
                canonical = _factories_to_canonical(row)
                prev = _last_snapshot["factories"].get(native_id)
                changed = _delta(prev, canonical)
                _last_snapshot["factories"][native_id] = canonical

                if changed:
                    ubid = _resolve_ubid_for("factories", native_id)
                    if not ubid:
                        log.warning(f"factories change for {native_id}: no UBID mapping")
                        continue
                    log.info(f"factories Δ for {ubid}: {list(changed.keys())}")
                    propagate_dept_event(
                        {
                            "source": "factories",
                            "ubid": ubid,
                            "pan": row["biz_pan"],
                            "changed_fields": changed,
                            "occurred_at": row.get("last_modified") or datetime.now(timezone.utc).isoformat(),
                        }
                    )
        except Exception as e:
            log.warning(f"factories poller error: {e}")
        await asyncio.sleep(POLL_INTERVAL_SEC)


async def poll_shops():
    """Snapshot polling + delta comparison. Real e-Karmika would offer
    no API better than a CSV dump — this is the worst-case integration
    surface and we want to show it works."""
    log.info("shops poller starting")
    while True:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{SHOPS_URL}/establishments")
                r.raise_for_status()
                rows = r.json()

            for row in rows:
                native_id = row["registration_no"]
                canonical = _shops_to_canonical(row)
                prev = _last_snapshot["shops"].get(native_id)
                changed = _delta(prev, canonical)
                _last_snapshot["shops"][native_id] = canonical

                if changed:
                    ubid = _resolve_ubid_for("shops", native_id)
                    if not ubid:
                        log.warning(f"shops change for {native_id}: no UBID mapping")
                        continue
                    log.info(f"shops Δ for {ubid}: {list(changed.keys())}")
                    propagate_dept_event(
                        {
                            "source": "shops",
                            "ubid": ubid,
                            "pan": row["pan_no"],
                            "changed_fields": changed,
                            "occurred_at": row.get("last_changed") or datetime.now(timezone.utc).isoformat(),
                        }
                    )
        except Exception as e:
            log.warning(f"shops poller error: {e}")
        await asyncio.sleep(POLL_INTERVAL_SEC)


def start_pollers(loop: asyncio.AbstractEventLoop):
    """Schedule both pollers as background tasks."""
    loop.create_task(poll_factories())
    loop.create_task(poll_shops())

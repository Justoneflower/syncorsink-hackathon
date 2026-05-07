"""
Factories adapter (FBIS / esuraksha schema).

Translation rules:
- SWS structured address {line1, line2, city, district, state, pincode}
  → single uppercase string, comma-joined (matches real esuraksha forms)
- SWS authorised_signatory.name → occupier_name
- PAN comes through unchanged (modulo case)
"""
import os
from typing import Any, Dict

import httpx

from .base import DeptAdapter

FACTORIES_URL = os.environ.get("FACTORIES_URL", "http://factories-mock:8002")


class FactoriesAdapter(DeptAdapter):
    name = "factories"

    def translate_address_change(self, sws_address: Dict[str, Any]) -> Dict[str, Any]:
        # Real esuraksha forms use a single-string address. Join the parts,
        # uppercase, strip empties.
        parts = [
            sws_address.get("line1", ""),
            sws_address.get("line2", ""),
            sws_address.get("city", ""),
        ]
        addr = ", ".join(p.strip() for p in parts if p and p.strip())
        pincode = sws_address.get("pincode", "")
        if pincode:
            addr = f"{addr} - {pincode}"
        return {"premises_address": addr.upper()}

    def translate_signatory_change(self, sws_signatory: Dict[str, Any]) -> Dict[str, Any]:
        # SWS distinguishes signatory.name from designation.
        # Factories only tracks occupier_name (the Factories Act's term).
        return {"occupier_name": sws_signatory.get("name", "").upper()}

    def write(self, pan: str, dept_native_payload: Dict[str, Any]) -> Dict[str, Any]:
        with httpx.Client(timeout=5.0) as client:
            r = client.patch(
                f"{FACTORIES_URL}/factories/by-pan/{pan}",
                json=dept_native_payload,
            )
            r.raise_for_status()
            return r.json()

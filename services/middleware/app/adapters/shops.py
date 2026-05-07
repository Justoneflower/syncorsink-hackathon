"""
Shops adapter (e-Karmika schema).

Translation rules:
- SWS structured address → semi-structured {door_no, street, locality, city, pin}
  - door_no comes from SWS line1 (first comma-segment if present)
  - street from line1 (rest) or line2
  - locality from line2
  - city as-is, pin from pincode
- SWS authorised_signatory.name → employer_name
- pan → pan_no (note the underscore, e-Karmika's convention)
"""
import os
from typing import Any, Dict

import httpx

from .base import DeptAdapter

SHOPS_URL = os.environ.get("SHOPS_URL", "http://shops-mock:8003")


def _split_address(line1: str, line2: str) -> Dict[str, str]:
    """Best-effort split of SWS line1/line2 into door_no + street + locality.

    Real e-Karmika forms ask for these separately. Real-world data is messy,
    so we use simple heuristics: first comma-segment of line1 → door_no,
    remainder → street, line2 → locality.
    """
    line1 = (line1 or "").strip()
    line2 = (line2 or "").strip()

    if "," in line1:
        door_no, _, rest = line1.partition(",")
        door_no = door_no.strip()
        street = rest.strip()
    else:
        # No comma — treat line1 as door_no, leave street to line2 if present
        door_no = line1
        street = ""

    if not street and line2:
        street = line2
        locality = ""
    else:
        locality = line2

    return {"door_no": door_no, "street": street, "locality": locality}


class ShopsAdapter(DeptAdapter):
    name = "shops"

    def translate_address_change(self, sws_address: Dict[str, Any]) -> Dict[str, Any]:
        split = _split_address(sws_address.get("line1", ""), sws_address.get("line2", ""))
        return {
            "address_of_establishment": {
                "door_no": split["door_no"],
                "street": split["street"],
                "locality": split["locality"],
                "city": sws_address.get("city", ""),
                "pin": sws_address.get("pincode", ""),
            }
        }

    def translate_signatory_change(self, sws_signatory: Dict[str, Any]) -> Dict[str, Any]:
        return {"employer_name": sws_signatory.get("name", "")}

    def write(self, pan: str, dept_native_payload: Dict[str, Any]) -> Dict[str, Any]:
        with httpx.Client(timeout=5.0) as client:
            r = client.patch(
                f"{SHOPS_URL}/establishments/by-pan/{pan}",
                json=dept_native_payload,
            )
            r.raise_for_status()
            return r.json()

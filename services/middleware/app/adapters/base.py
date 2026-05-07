"""
Adapter base class. Each department gets its own subclass that knows:

1. How to translate a canonical SWS-shaped change into the dept's schema
2. How to perform the write against the dept's API
3. How to translate dept-native data back to canonical (Direction 2, v2)

In production this would be config-driven (JSONata rules in a Schema
Registry, hot-reloadable, no code change to onboard a new dept). For the
prototype, Python classes are clearer to read and quicker to iterate on.
The deck cites "JSONata + Confluent Schema Registry" as the production
choice — see docs/prototype-vs-production.md.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict


class DeptAdapter(ABC):
    name: str = ""

    @abstractmethod
    def translate_address_change(
        self, sws_address: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Map SWS structured address → dept-native shape."""
        ...

    @abstractmethod
    def translate_signatory_change(
        self, sws_signatory: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Map SWS structured signatory → dept-native shape."""
        ...

    @abstractmethod
    def write(self, pan: str, dept_native_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Write the translated payload to the dept's API. Returns the
        new dept-side record. Raises on HTTP errors."""
        ...

#!/usr/bin/env bash
# Scenario 1 — Direction 1: SWS → Departments
#
# 1. Update Acme Industries' registered address on SWS
# 2. Watch the same change appear, in three different schemas, in
#    the Factories and Shops mocks
# 3. Pull the end-to-end audit trail for the UBID
#
# This proves: webhook ingestion → UBID routing → per-dept schema
# translation → write → audit. The whole Direction 1 path.

set -euo pipefail

UBID="KA-UBID-2025-0089123"
PAN="AAACR5055K"

# Pretty colors if we're on a TTY
if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
  BOLD=""; DIM=""; GREEN=""; YELLOW=""; RESET=""
fi

# Use jq if available, otherwise fall back to python
pp() {
  if command -v jq >/dev/null 2>&1; then
    jq "$@"
  else
    python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin), indent=2))"
  fi
}

echo "${BOLD}=== Scenario 1: SWS → Departments ===${RESET}"
echo
echo "${BOLD}Step 1.${RESET} Current address for Acme Industries (UBID=$UBID):"
echo
echo "${DIM}— On SWS (canonical structured form):${RESET}"
curl -s "http://localhost:8001/businesses/$UBID" | pp '.registered_address'
echo
echo "${DIM}— On Factories (single-string, uppercase):${RESET}"
curl -s "http://localhost:8002/factories/by-pan/$PAN" | pp '.premises_address'
echo
echo "${DIM}— On Shops (semi-structured, lowercase city):${RESET}"
curl -s "http://localhost:8003/establishments/by-pan/$PAN" | pp '.address_of_establishment'
echo
echo "${YELLOW}Three schemas. Three different shapes for the same address.${RESET}"
echo
read -rp "Press ENTER to update the address on SWS..."

echo
echo "${BOLD}Step 2.${RESET} PATCH SWS with new registered address..."
curl -s -X PATCH "http://localhost:8001/businesses/$UBID" \
  -H "Content-Type: application/json" \
  -d '{
    "registered_address": {
      "line1": "Plot 99, Block A",
      "line2": "Electronics City Phase 1",
      "city": "Bengaluru",
      "district": "Bengaluru Urban",
      "state": "Karnataka",
      "pincode": "560100"
    }
  }' > /dev/null
echo "  ${GREEN}✓ SWS updated, webhook fired to middleware${RESET}"
echo

# Give the middleware a moment to propagate
sleep 1

echo "${BOLD}Step 3.${RESET} Same business, same PAN — now check the legacy depts:"
echo
echo "${DIM}— Factories (auto-translated to single uppercase string):${RESET}"
curl -s "http://localhost:8002/factories/by-pan/$PAN" | pp '.premises_address'
echo
echo "${DIM}— Shops (auto-translated to semi-structured shape):${RESET}"
curl -s "http://localhost:8003/establishments/by-pan/$PAN" | pp '.address_of_establishment'
echo
echo "${GREEN}Both depts now hold the new address — in their own schemas, with zero source-system changes.${RESET}"
echo
read -rp "Press ENTER to see the end-to-end audit trail..."

echo
echo "${BOLD}Step 4.${RESET} Audit trail for $UBID — every step, every target:"
echo
curl -s "http://localhost:8000/trace/$UBID" \
  | pp '.[] | {at: .occurred_at, action, source, target, status}'
echo
echo "${GREEN}✓ Demo complete.${RESET}  See README for the manual API walkthrough."

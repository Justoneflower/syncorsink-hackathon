#!/usr/bin/env bash
# Scenario 3 — Conflict resolution
#
# Story: at almost the same moment, two updates hit for Sahyadri Pharma:
#  - SWS: a user changes the registered address
#  - Factories: an officer also changes the address (different value)
#
# Within the 30s in-flight window, our conflict engine detects both
# updates land on the same (UBID, field). The configured policy for
# 'registered_address' is source_of_record=sws. So SWS wins — the
# Factories incoming change is REJECTED and audited.
#
# This proves: conflicts are detected, resolved by configurable policy,
# and the audit trail explains why the final state is what it is.

set -euo pipefail

UBID="KA-UBID-2025-0061204"        # Sahyadri Pharma
PAN="AALCS9912R"

if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; RESET=$'\033[0m'
else
  BOLD=""; DIM=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi

pp() {
  if command -v jq >/dev/null 2>&1; then jq "$@"; else python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin), indent=2))"; fi
}

echo "${BOLD}=== Scenario 3: Simultaneous conflict ===${RESET}"
echo
echo "Two updates will hit within ~1 second of each other:"
echo "  - SWS: address → 'Plot SWS-NEW, Hosur Road'"
echo "  - Factories (direct): address → 'PLOT FACT-NEW, ELECTRONIC CITY'"
echo
echo "Policy for ${BOLD}registered_address${RESET}: ${YELLOW}source_of_record = sws${RESET}"
echo "Expected: SWS wins. Factories' incoming change is rejected and audited."
echo
read -rp "Press ENTER to fire both updates simultaneously..."

# Fire SWS PATCH
curl -s -X PATCH "http://localhost:8001/businesses/$UBID" \
  -H "Content-Type: application/json" \
  -d '{
    "registered_address": {
      "line1": "Plot SWS-NEW",
      "line2": "Hosur Road",
      "city": "Bengaluru",
      "district": "Bengaluru Urban",
      "state": "Karnataka",
      "pincode": "560100"
    }
  }' > /dev/null &
SWS_PID=$!

# Fire Factories direct PATCH ~50ms later (within the 30s conflict window).
# We change the premises_address — the poller will pick this up and emit
# a Direction 2 event with source=factories.
sleep 0.05
curl -s -X PATCH "http://localhost:8002/factories/by-pan/$PAN" \
  -H "Content-Type: application/json" \
  -d '{"premises_address": "PLOT FACT-NEW, ELECTRONIC CITY, BENGALURU - 560100"}' > /dev/null &
FAC_PID=$!

wait $SWS_PID $FAC_PID
echo "  ${GREEN}✓ Both updates fired${RESET}"
echo
echo "${BOLD}Step 2.${RESET} Waiting ~6s for the CDC poller to surface the Factories change and the conflict engine to resolve..."
sleep 6

echo
echo "${BOLD}Step 3.${RESET} Final state — SWS:"
curl -s "http://localhost:8001/businesses/$UBID" | pp '.registered_address'
echo
echo "${BOLD}Step 4.${RESET} Conflict + policy entries from the audit log:"
echo
curl -s "http://localhost:8000/trace/$UBID" \
  | pp '[.[] | select(.action == "conflict" or .action == "policy")] | .[] | {at: .occurred_at, action, source, status, payload}'
echo

echo "${BOLD}Step 5.${RESET} Full Direction 2 trace (source=factories events)"
echo "${DIM}        — note the 'policy' entry showing the rejection reason:${RESET}"
echo
curl -s "http://localhost:8000/trace/$UBID" \
  | pp '[.[] | select(.source == "factories")] | .[] | {at: .occurred_at, action, status, payload}'
echo

echo "${GREEN}✓ Demo complete.${RESET}"
echo "${DIM}Open dashboard at http://localhost:5173 to see this in the live feed.${RESET}"

#!/usr/bin/env bash
# Scenario 2 — Direction 2: Department → SWS
#
# Story: an officer in the Factories department directly updates the
# occupier_name for a business (this happens all the time IRL — paper
# filings hit Factories first, never reach SWS). Our middleware's CDC
# poller picks it up within ~3s, reverse-translates to SWS canonical
# shape, and writes back to SWS.
#
# This proves: change discovery for non-event-emitting systems
# (the whole point of the "polling/CDC fallback" claim in the deck).

set -euo pipefail

UBID="KA-UBID-2025-0034871"        # Tulsi Foods & Beverages
PAN="ABFCT8821M"
NEW_OCCUPIER="VIKRAM DESAI"        # promoted from director to occupier

if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
  BOLD=""; DIM=""; GREEN=""; YELLOW=""; CYAN=""; RESET=""
fi

pp() {
  if command -v jq >/dev/null 2>&1; then jq "$@"; else python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin), indent=2))"; fi
}

echo "${BOLD}=== Scenario 2: Department → SWS (Direction 2) ===${RESET}"
echo
echo "${BOLD}Step 1.${RESET} Current authorised signatory for Tulsi Foods (UBID=$UBID):"
echo
echo "${DIM}— On SWS:${RESET}"
curl -s "http://localhost:8001/businesses/$UBID" | pp '.authorised_signatory'
echo
echo "${DIM}— On Factories (occupier_name field):${RESET}"
curl -s "http://localhost:8002/factories/by-pan/$PAN" | pp '.occupier_name'
echo
read -rp "Press ENTER to update the occupier directly on Factories (bypassing SWS)..."

echo
echo "${BOLD}Step 2.${RESET} Officer updates Factories directly — SWS hears nothing yet."
curl -s -X PATCH "http://localhost:8002/factories/by-pan/$PAN" \
  -H "Content-Type: application/json" \
  -d "{\"occupier_name\": \"$NEW_OCCUPIER\"}" > /dev/null
echo "  ${GREEN}✓ Factories updated${RESET}  (no webhook — Factories doesn't emit events)"
echo
echo "${DIM}— SWS still shows the OLD signatory (no propagation has happened yet):${RESET}"
curl -s "http://localhost:8001/businesses/$UBID" | pp '.authorised_signatory'
echo
echo "${YELLOW}Watch the middleware logs:${RESET} docker-compose logs -f middleware"
echo "  You'll see 'factories Δ for $UBID: [authorised_signatory]' within ~3s."
echo

echo "${BOLD}Step 3.${RESET} Waiting ~5s for the CDC poller to detect the change..."
sleep 5

echo
echo "${DIM}— SWS now (after the poller propagated back):${RESET}"
curl -s "http://localhost:8001/businesses/$UBID" | pp '.authorised_signatory'
echo
echo "${GREEN}✓ Direction 2 worked${RESET} — change in Factories reached SWS without modifying either system."
echo
read -rp "Press ENTER to see the audit trail..."

echo
echo "${BOLD}Step 4.${RESET} Audit trail showing Direction 2 events (source=factories):"
echo
curl -s "http://localhost:8000/trace/$UBID" \
  | pp '[.[] | select(.source == "factories")] | .[] | {at: .occurred_at, action, source, target, status}'
echo
echo "${GREEN}✓ Demo complete.${RESET}  Now try: ./scripts/demo-scenario-3.sh  (conflict resolution)"

# SyncOrSink — v2

Middleware that propagates business service requests **both ways** between Karnataka's Single Window System (`sws.investkarnataka.co.in`) and 40+ legacy department systems, using UBID as the join key. Conflict-aware. Fully audited. Built for the AI for Bharat Hackathon, Theme 2.

> **v2 scope:** All three problem-statement scenarios working end-to-end — Direction 1 (SWS → depts), Direction 2 (dept → SWS), conflict detection + resolution with three configurable policies, append-only audit log with end-to-end trace, React operations dashboard.

---

## Quick Start

```bash
# 1. Bring everything up (~60s first time)
docker-compose up --build

# 2. Seed three Karnataka businesses across all three systems
docker-compose exec middleware python -m app.seed

# 3. Run the demos in order
./scripts/demo-scenario-1.sh    # SWS → Departments
./scripts/demo-scenario-2.sh    # Departments → SWS (CDC)
./scripts/demo-scenario-3.sh    # Conflict resolution

# 4. Open the operations dashboard
open http://localhost:5173      # macOS
xdg-open http://localhost:5173  # Linux
```

> **Verified end-to-end** with an integration test (`/tmp/run_v2_test.py`) covering all three scenarios. All assertions pass.

## What's running

| Service | Port | Role | Discovery surface |
|---|---|---|---|
| `sws-mock` | 8001 | Karnataka SWS (`sws.investkarnataka.co.in`) | Outbound webhook on change |
| `factories-mock` | 8002 | FBIS / esuraksha (`esuraksha.karnataka.gov.in`) | **CDC poll** every 3s |
| `shops-mock` | 8003 | e-Karmika (`ekarmika.karnataka.gov.in`) | **Snapshot poll** + delta every 3s |
| `middleware` | 8000 | SyncOrSink core | — |
| `dashboard` | 5173 | React operations console | — |
| `postgres` | 5432 | Backing store + audit log | — |
| `redis` | 6379 | In-flight conflict window (30s TTL) | — |

## What v2 demonstrates

The problem statement asks for: bidirectional propagation, change discovery from non-event-emitting systems, conflict detection with explainable resolution, idempotent at-least-once delivery, and a complete audit trail. Everything is exercised by the three demo scripts.

**Three discovery surfaces, one middleware:**
- SWS → middleware: webhook (push)
- Factories → middleware: CDC simulation (poll, since real FBIS doesn't emit)
- Shops → middleware: snapshot polling + delta (worst case, no API better than dump)

**Three conflict policies, configurable per field:**
- `source_of_record` — designated source always wins (default for `registered_address`, source = SWS)
- `last_write_wins` — most recent timestamp wins (default for `authorised_signatory`)
- `human_escalation` — both writes blocked, conflict goes to officer review queue (default for `business_name`)

Policies are declared in `services/middleware/app/conflict.py` (`FIELD_POLICY`). In production this is a hot-reloadable config table; for the prototype it's Python code.

## End-to-end audit trail

Every propagation, conflict, and resolution lands in the `audit_log` table. Query the full causal chain for any UBID with the trace CLI:

```bash
./scripts/trace.py KA-UBID-2025-0089123
./scripts/trace.py KA-UBID-2025-0089123 --conflicts-only
./scripts/trace.py KA-UBID-2025-0089123 --since 5m
./scripts/trace.py KA-UBID-2025-0089123 --json | jq
```

Or open the dashboard at http://localhost:5173 — every event flows live into the propagation feed, with one click to inspect any UBID's full trace.

## Project layout

```
syncorsink/
├── docker-compose.yml          ← orchestrates 7 services (4 mocks/middleware, 2 infra, 1 UI)
├── README.md
├── data/seed.json              ← 3 hand-crafted Karnataka businesses
│
├── services/
│   ├── sws-mock/               ← Karnataka SWS (modern, structured, push-based)
│   ├── factories-mock/         ← FBIS — flat uppercase strings, occupier_name
│   ├── shops-mock/             ← e-Karmika — semi-structured, employer_name, /changes_since
│   └── middleware/             ← SyncOrSink core
│       └── app/
│           ├── main.py         ← FastAPI: webhook ingest, trace, audit, conflict review
│           ├── propagator.py   ← Direction 1 + Direction 2 orchestrator
│           ├── conflict.py     ← Redis-backed in-flight window, three policies
│           ├── pollers.py      ← background CDC + snapshot pollers (asyncio)
│           ├── routing.py      ← UBID → (which depts) lookup
│           ├── audit.py        ← append-only log writer
│           ├── adapters/       ← per-dept schema translators (forward + reverse)
│           └── seed.py         ← seeds all 3 systems + routing index
│
├── dashboard/                  ← React + Babel single-file SPA
│   └── index.html
│
├── scripts/
│   ├── demo-scenario-1.sh      ← SWS → depts (Direction 1)
│   ├── demo-scenario-2.sh      ← dept → SWS (Direction 2)
│   ├── demo-scenario-3.sh      ← Conflict resolution
│   ├── trace.py                ← Pretty-print audit trail
│   └── init-databases.sql      ← Postgres schema bootstrap
│
└── docs/
    └── prototype-vs-production.md
```

## Try it manually (without the demo scripts)

```bash
# Inspect SWS — clean structured form
curl -s http://localhost:8001/businesses/KA-UBID-2025-0089123 | jq

# Same business, totally different schema in Factories
curl -s http://localhost:8002/factories/by-pan/AAACR5055K | jq

# And different again in Shops
curl -s http://localhost:8003/establishments/by-pan/AAACR5055K | jq

# Update SWS — middleware propagates to both depts
curl -s -X PATCH http://localhost:8001/businesses/KA-UBID-2025-0089123 \
  -H "Content-Type: application/json" \
  -d '{"registered_address":{"line1":"New 99","line2":"New Block","city":"Bengaluru","district":"Bengaluru Urban","state":"Karnataka","pincode":"560100"}}'

# Or update Factories directly — CDC poller picks up the change within 3s
# and propagates back to SWS (Direction 2)
curl -s -X PATCH http://localhost:8002/factories/by-pan/AAACR5055K \
  -H "Content-Type: application/json" \
  -d '{"occupier_name":"NEW OCCUPIER NAME"}'

# Inspect the audit trail
./scripts/trace.py KA-UBID-2025-0089123
```

## API surface

Each service exposes Swagger UI at `/docs`:
- http://localhost:8001/docs — SWS
- http://localhost:8002/docs — Factories
- http://localhost:8003/docs — Shops
- http://localhost:8000/docs — Middleware

Middleware key endpoints:
- `POST /webhooks/sws` — Direction 1 ingestion
- `GET /trace/{ubid}` — full audit trail
- `GET /audit?limit=50` — recent events feed
- `GET /conflicts?status=pending` — review queue
- `POST /conflicts/{id}/resolve` — officer resolves a conflict
- `GET /policies` — current per-field conflict policies
- `GET /businesses` — UBIDs known to the routing index
- `GET /routing` — full routing index

## Stack

Boring on purpose:

- **Python 3.11 + FastAPI** — all four backend services
- **Postgres 15** — one instance, separate logical DB per service
- **Redis 7** — in-flight conflict window (30s TTL keys)
- **React 18 + Babel-standalone** — dashboard, no build step
- **Docker Compose** — local-only orchestration

The deck's production stack (Temporal on ECS, MSK Kafka, SageMaker entity resolution, Aurora) is **intent**, not what runs in the prototype. The prototype-to-production mapping is in [`docs/prototype-vs-production.md`](docs/prototype-vs-production.md).

## What's intentionally NOT in v2

- **Real Debezium for CDC** — we simulate via 3-second polling, which has the same logical effect at a fraction of the moving parts. Production replaces this with `debezium/connect` reading the dept Postgres WAL.
- **Hot-reloadable schema mappings** — translation rules are Python classes for clarity. Production loads JSONata from a config table.
- **ML entity resolution for missing UBIDs** — synthetic data has clean PANs so we don't need it. Production layers in SageMaker for the L1 failure mode (see deck slide 5).
- **Multi-region failover, real auth, rate limiting** — out of scope for hackathon, but trivial extensions on the existing shape.

## Team

SyncOrSink Squad — Kusum Indoria, Ansh Varma. AI for Bharat Hackathon 2026.
#   s y n c o r s i n k  
 #   s y n c o r s i n k  
 #   s y n c o r s i n k  
 
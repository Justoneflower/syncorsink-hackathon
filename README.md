# SyncOrSink

v2 Middleware that propagates business service requests both ways between Karnataka's Single Window System (`sws.investkarnataka.co.in`) and 40+ legacy department systems, using UBID as the join key.

Conflict aware. Fully audited.

Built for the AI for Bharat Hackathon, Theme 2.

> v2 scope: All three problem statement scenarios working end to end  
> Direction 1 (SWS → departments)  
> Direction 2 (department → SWS)  
> Conflict detection and resolution with three configurable policies  
> Append only audit log with end to end trace  
> React operations dashboard

## Quick Start

```bash
# 1. Bring everything up (~60s first time)
docker-compose up --build

# 2. Seed three Karnataka businesses across all three systems
docker-compose exec middleware python -m app.seed

# 3. Run the demos in order
./scripts/demo-scenario-1.sh
./scripts/demo-scenario-2.sh
./scripts/demo-scenario-3.sh

# 4. Open the operations dashboard
open http://localhost:5173
xdg-open http://localhost:5173
```

> Verified end to end with an integration test (`/tmp/run_v2_test.py`) covering all three scenarios. All assertions pass.

## What's Running

| Service | Port | Role | Discovery Surface |
|---|---|---|---|
| `sws-mock` | 8001 | Karnataka SWS (`sws.investkarnataka.co.in`) | Outbound webhook on change |
| `factories-mock` | 8002 | FBIS / esuraksha (`esuraksha.karnataka.gov.in`) | CDC poll every 3s |
| `shops-mock` | 8003 | e-Karmika (`ekarmika.karnataka.gov.in`) | Snapshot poll + delta every 3s |
| `middleware` | 8000 | SyncOrSink core | — |
| `dashboard` | 5173 | React operations console | — |
| `postgres` | 5432 | Backing store + audit log | — |
| `redis` | 6379 | In flight conflict window (30s TTL) | — |

## What v2 Demonstrates

The problem statement asks for:

Bidirectional propagation  
Change discovery from non event emitting systems  
Conflict detection with explainable resolution  
Idempotent at least once delivery  
Complete audit trail

Everything is exercised by the three demo scripts.

### Three discovery surfaces, one middleware

SWS → middleware: webhook (push)

Factories → middleware: CDC simulation (poll, since real FBIS doesn't emit)

Shops → middleware: snapshot polling + delta (worst case, no API better than dump)

### Three conflict policies, configurable per field

`source_of_record`

Designated source always wins  
Default for `registered_address`  
Source = SWS

`last_write_wins`

Most recent timestamp wins  
Default for `authorised_signatory`

`human_escalation`

Both writes blocked  
Conflict goes to officer review queue  
Default for `business_name`

Policies are declared in `services/middleware/app/conflict.py` (`FIELD_POLICY`).

In production this is a hot reloadable config table. For the prototype it's Python code.

## End to End Audit Trail

Every propagation, conflict, and resolution lands in the `audit_log` table.

Query the full causal chain for any UBID with the trace CLI:

```bash
./scripts/trace.py KA-UBID-2025-0089123

./scripts/trace.py KA-UBID-2025-0089123 --conflicts-only

./scripts/trace.py KA-UBID-2025-0089123 --since 5m

./scripts/trace.py KA-UBID-2025-0089123 --json | jq
```

Or open the dashboard at:

```txt
http://localhost:5173
```

Every event flows live into the propagation feed, with one click to inspect any UBID's full trace.

## Project Layout

```txt
syncorsink/

├── docker-compose.yml
├── README.md
├── data/
│   └── seed.json

├── services/
│   ├── sws-mock/
│   ├── factories-mock/
│   ├── shops-mock/
│   └── middleware/
│       └── app/
│           ├── main.py
│           ├── propagator.py
│           ├── conflict.py
│           ├── pollers.py
│           ├── routing.py
│           ├── audit.py
│           ├── adapters/
│           └── seed.py

├── dashboard/
│   └── index.html

├── scripts/
│   ├── demo-scenario-1.sh
│   ├── demo-scenario-2.sh
│   ├── demo-scenario-3.sh
│   ├── trace.py
│   └── init-databases.sql

└── docs/
    └── prototype-vs-production.md
```

## Try It Manually

```bash
# Inspect SWS
curl -s http://localhost:8001/businesses/KA-UBID-2025-0089123 | jq

# Same business in Factories
curl -s http://localhost:8002/factories/by-pan/AAACR5055K | jq

# Same business in Shops
curl -s http://localhost:8003/establishments/by-pan/AAACR5055K | jq

# Update SWS
curl -s -X PATCH http://localhost:8001/businesses/KA-UBID-2025-0089123 \
-H "Content-Type: application/json" \
-d '{"registered_address":{"line1":"New 99","line2":"New Block","city":"Bengaluru","district":"Bengaluru Urban","state":"Karnataka","pincode":"560100"}}'

# Update Factories directly
curl -s -X PATCH http://localhost:8002/factories/by-pan/AAACR5055K \
-H "Content-Type: application/json" \
-d '{"occupier_name":"NEW OCCUPIER NAME"}'

# Inspect audit trail
./scripts/trace.py KA-UBID-2025-0089123
```

## API Surface

Each service exposes Swagger UI at `/docs`.

```txt
http://localhost:8001/docs
http://localhost:8002/docs
http://localhost:8003/docs
http://localhost:8000/docs
```

### Middleware Key Endpoints

`POST /webhooks/sws`

Direction 1 ingestion

`GET /trace/{ubid}`

Full audit trail

`GET /audit?limit=50`

Recent events feed

`GET /conflicts?status=pending`

Review queue

`POST /conflicts/{id}/resolve`

Officer resolves a conflict

`GET /policies`

Current per field conflict policies

`GET /businesses`

UBIDs known to the routing index

`GET /routing`

Full routing index

## Stack

Python 3.11 + FastAPI  
All four backend services

Postgres 15  
One instance, separate logical DB per service

Redis 7  
In flight conflict window (30s TTL keys)

React 18 + Babel standalone  
Dashboard with no build step

Docker Compose  
Local only orchestration

The deck's production stack (Temporal on ECS, MSK Kafka, SageMaker entity resolution, Aurora) is intent, not what runs in the prototype.

The prototype to production mapping is in:

```txt
docs/prototype-vs-production.md
```

## What's Intentionally Not in v2

Real Debezium for CDC

We simulate via 3 second polling, which has the same logical effect at a fraction of the moving parts.

Production replaces this with `debezium/connect` reading the department Postgres WAL.

Hot reloadable schema mappings

Translation rules are Python classes for clarity.

Production loads JSONata from a config table.

ML entity resolution for missing UBIDs

Synthetic data has clean PANs so we don't need it.

Production layers in SageMaker for the L1 failure mode.

Multi region failover, real auth, rate limiting

Out of scope for hackathon, but trivial extensions on the existing shape.

## Team

SyncOrSink Squad

Kusum Indoria  
Ansh Varma

AI for Bharat Hackathon 2026

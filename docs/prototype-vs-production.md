# Prototype vs. Production

The hackathon prototype is intentionally simpler than the production architecture in the deck. This isn't laziness — it's correct scoping. Boring, observable infrastructure for the prototype; managed, durable infrastructure for production.

| Component | Prototype (this repo) | Production (deck) | Why the gap |
|---|---|---|---|
| Workflow engine | asyncio + retry decorators in `propagator.py` | Temporal on ECS Fargate | Durable workflow state matters at scale; not at 3 mocks |
| Event bus | Direct HTTP webhook + Postgres LISTEN/NOTIFY (v2) | Amazon MSK (Kafka) | MSK gives multi-consumer fan-out and replay; not needed in v1 |
| Schema translation | Python adapter classes (`adapters/*.py`) | JSONata rules in Confluent Schema Registry, hot-reload | Code is faster to iterate at hackathon scope; config-driven enables onboarding new depts in <1 day |
| Idempotency store | SHA-256 keys (computed, not enforced in v1) | Redis ElastiCache, SETNX with TTL | Wired in v1, enforced in v2 |
| Conflict window | Not yet (v2) | Redis 30s TTL window per UBID | Implementing in v2 |
| CDC for non-event-emitting depts | HTTP poll endpoint on Shops mock (v2) | Debezium tailing dept Postgres logs | Debezium adds a moving part; v2 will demo it on Factories |
| Entity resolution (UBID gap) | Deterministic PAN exact-match in seed | SageMaker entity resolution model | Synthetic data has clean PANs; production has 30 years of typos |
| Audit store | Postgres `audit_log` table | Aurora PostgreSQL with read replicas | Same logical design, different operational story |
| Deployment | `docker-compose up` | AWS-native (see deck Slide 11) | Hackathon judges run locally |

## What does NOT change between prototype and production

- The architectural shape (webhook ingest → UBID routing → translate → write → audit) is identical.
- The append-only audit log invariant is identical.
- The "no source-system modifications" non-negotiable is identical.
- The plug-in adapter pattern (one class per dept) is identical — production just loads them from config instead of code.

This mapping is what we mean by "prototype-ready, production-shaped". You can read the prototype code and see exactly where each AWS service in the deck would slot in.

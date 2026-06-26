# Production Infrastructure Baseline

This project now supports two job backends:

- `memory`: local development fallback, using FastAPI background tasks.
- `durable`: production baseline, using PostgreSQL for job truth and Redis for worker notifications.

## Durable job flow

```text
POST /api/v1/start-workflow
↓
Create Project row in PostgreSQL
↓
Attach durable Job row to the project
↓
Push job_id to Redis queue
↓
workflow-worker claims job with a database lease
↓
Worker runs Stage 1 → Stage 4
↓
Progress/result/failure is persisted in PostgreSQL
↓
GET /api/v1/jobs/{job_id}
```

Redis is not the source of truth. If Redis loses a notification, the job row still exists. If a worker dies, its lease expires and another worker can requeue the job.

## Core tables

- `projects`: stable project records and workflow state.
- `jobs`: durable workflow jobs, status, payload, lease, attempts, result and errors.
- `shots`: production shot-level state target.
- `assets`: generated and uploaded media records.
- `reviews`: structured human/model review records.
- `failure_events`: standardized failure taxonomy records.

## Local Docker stack

```bash
cp .env.example .env
docker compose up -d --build
```

Compose starts:

- PostgreSQL
- Redis
- one migration container
- FastAPI service
- workflow worker

Readiness:

```bash
curl http://localhost:8000/ready
```

When `JOB_BACKEND=durable`, `/ready` checks PostgreSQL and Redis in addition to core runtime config.

## API access control

Production defaults to API-key authentication:

- `API_AUTH_ENABLED=true`
- `API_KEYS=<comma-separated internal keys>`

Every route except `/health` and `/ready` requires either `X-API-Key` or
`Authorization: Bearer`. This protects endpoints that can trigger model spend or
expose project assets/status.

## Migration

```bash
alembic upgrade head
```

For local migration smoke tests without PostgreSQL:

```bash
mkdir -p tmp
DATABASE_URL=sqlite+aiosqlite:///./tmp/alembic_check.sqlite alembic upgrade head
```

## Data backend strategy

PostgreSQL is the production default (`DATA_BACKEND=postgres`). Existing workflow
stages still import `AirtableService` for compatibility, but that name now resolves
to a PostgreSQL-backed implementation unless `DATA_BACKEND=airtable` is explicitly
set.

Airtable is retained only as a legacy adapter for older demos. The production
direction is a low-cost stack:

- PostgreSQL: projects, jobs, shots, assets, reviews, failures.
- Redis: queue notification, not source of truth.
- OSS/S3-compatible storage: large video/image assets.
- A minimal internal review dashboard/API instead of Airtable as the human review
  surface.

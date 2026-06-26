# Production Infrastructure Baseline

This project now supports two job backends:

- `memory`: local development fallback, using FastAPI background tasks.
- `durable`: production baseline, using PostgreSQL for job truth and Redis for worker notifications.

## Durable job flow

```text
POST /api/v1/start-workflow
↓
Create Airtable project record
↓
Create Project + Job rows in PostgreSQL
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

- `projects`: stable project records, linked to Airtable during migration.
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

## Migration

```bash
alembic upgrade head
```

For local migration smoke tests without PostgreSQL:

```bash
mkdir -p tmp
DATABASE_URL=sqlite+aiosqlite:///./tmp/alembic_check.sqlite alembic upgrade head
```

## Current migration boundary

Airtable remains the operational review interface. PostgreSQL now owns durable job state and contains the target production schema. Subsequent work should progressively move project, shot, asset, review and failure writes from Airtable-only into PostgreSQL-first with optional Airtable sync.

# Deployment guide

## Supported modes

| Mode | Database | Scheduler | Intended use |
| --- | --- | --- | --- |
| Local Python | SQLite | In API process | Development and portfolio demo |
| Docker Compose | PostgreSQL | Separate worker | Production-shaped evaluation |
| Production | Managed PostgreSQL | Dedicated single worker | Small-team deployment |

## Local Python

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
pulsewatch init-db
uvicorn app.main:app --reload
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`. Environment
variables can be loaded by the shell or deployment platform; the application does
not silently parse `.env`.

## Docker Compose

Set at least an administrative key:

```bash
cp .env.example .env
pulsewatch generate-key
# Paste the value into PULSEWATCH_ADMIN_API_KEY in .env.
docker compose up --build
```

Verify:

```bash
curl --fail http://127.0.0.1:8000/api/v1/health/live
curl --fail http://127.0.0.1:8000/api/v1/health/ready
curl --fail http://127.0.0.1:8000/metrics
docker compose ps
```

The Compose topology contains:

- `postgres`: persistent relational storage;
- `api`: migrations, FastAPI, dashboard, and manual checks;
- `worker`: scheduled checks, incident notifications, and retention.

Compose defaults are for local evaluation. Change the PostgreSQL password and use a
secret manager for any shared or internet-accessible environment.

## Database migrations

Alembic is the schema authority outside local auto-create mode.

```bash
alembic current
alembic upgrade head
alembic history
```

To test rollback in a disposable environment:

```bash
alembic downgrade base
alembic upgrade head
```

Never run destructive downgrades against production without a reviewed recovery
plan. Back up the database before schema changes.

The container currently runs `alembic upgrade head` before Uvicorn. With more than
one API replica, move migration execution into a one-shot release job and start API
replicas only after it succeeds.

### Upgrading from the portfolio 1.x database

Version 2.0 introduces Alembic as a new baseline and is designed for a clean schema.
Do not apply the initial revision over a populated 1.x SQLite schema with the same
table names. Back up the old file, export the service definitions, initialize a new
2.0 database, and re-register those services. Check history from 1.x can be archived
separately. This explicit boundary is safer than pretending an untested in-place
conversion preserves operational history.

## Production topology

Recommended baseline:

1. managed PostgreSQL with encryption, backups, and restricted network access;
2. one or more stateless API replicas with
   `PULSEWATCH_SCHEDULER_ENABLED=false`;
3. exactly one worker process running `pulsewatch worker`;
4. TLS reverse proxy or ingress;
5. outbound allowlist/proxy for monitored endpoints and webhook destinations;
6. secret manager for API key, database credentials, and webhook secret;
7. Prometheus scraping over a private network;
8. centralized JSON log collection.

## Required production configuration

```bash
PULSEWATCH_ENVIRONMENT=production
PULSEWATCH_DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@HOST:5432/DB
PULSEWATCH_AUTO_CREATE_SCHEMA=false
PULSEWATCH_ADMIN_API_KEY=<at-least-16-characters>
PULSEWATCH_LOG_JSON=true
```

API process:

```bash
PULSEWATCH_SCHEDULER_ENABLED=false
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
```

Worker process:

```bash
PULSEWATCH_SCHEDULER_ENABLED=true
pulsewatch worker
```

Do not run multiple workers without implementing a distributed lease. They could
select the same due services before either advances `next_check_at`.

## Reverse proxy controls

The application emits CSP, frame, MIME-sniffing, referrer, and permissions headers.
The proxy should additionally:

- terminate TLS and redirect HTTP to HTTPS;
- set a request-size limit;
- rate-limit administrative mutation routes;
- restrict `/metrics` and operational history routes;
- preserve or generate `X-Request-ID`;
- use a trusted upstream network;
- apply an authentication layer if public read endpoints are inappropriate.

HSTS is emitted when the application sees an HTTPS request. Configure forwarded
headers only from trusted proxies so the scheme is interpreted correctly.

## Secrets

Generate independent, random values:

```bash
pulsewatch generate-key
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Rotate the administrative key by updating the secret and restarting API replicas.
Webhook consumers should support a short overlap window when rotating signing
secrets; PulseWatch currently accepts one active signing secret.

Never commit:

- administrative API keys;
- webhook signing secrets;
- production database URLs;
- database dumps;
- copied operator logs containing sensitive infrastructure names.

## Capacity and scaling

Important controls:

- `PULSEWATCH_CHECK_CONCURRENCY`: outbound concurrency within a batch;
- `PULSEWATCH_SCHEDULER_BATCH_SIZE`: services selected per cycle;
- `PULSEWATCH_SCHEDULER_POLL_SECONDS`: selection frequency;
- per-service timeout/retry/interval;
- PostgreSQL connection and resource limits.

A rough upper-bound request rate without retries is:

```math
rate \approx \sum_{service=1}^{n} \frac{1}{interval_{service}}
```

Retries and slow timeouts increase concurrent work. Load-test with representative
failure behavior; a healthy-only test understates resource use.

## Retention and backup

Check results older than `PULSEWATCH_RETENTION_DAYS` are deleted by the worker.
Incidents, audit events, and notification deliveries are retained until their parent
service is deleted or an external archival policy is applied.

For PostgreSQL:

- enable automated backups and point-in-time recovery where available;
- test restores, not just backups;
- monitor storage growth;
- archive audit data before introducing a purge policy.

For SQLite, stop writes or use a SQLite-safe backup operation rather than copying an
actively changing file blindly.

## Webhook networking

Keep `PULSEWATCH_ALLOW_PRIVATE_HOSTS=false` unless private targets are required and
the environment is trusted. Application validation cannot fully prevent DNS
rebinding. Enforce outbound IP/domain policy at the network layer.

See [Webhook contract](WEBHOOKS.md) for signing and retry behavior.

## Release checklist

- [ ] CI passes on Python 3.11 and 3.12.
- [ ] Coverage remains at or above 90%.
- [ ] Ruff, mypy, Bandit, pip-audit, and CodeQL pass.
- [ ] Alembic upgrades a clean PostgreSQL database.
- [ ] Database backup and rollback plan are reviewed.
- [ ] Secrets are present in the target secret manager.
- [ ] API scheduler is disabled and exactly one worker is configured.
- [ ] Liveness, readiness, metrics, logs, and webhook delivery are verified.
- [ ] TLS, egress restrictions, and `/metrics` access control are active.
- [ ] Changelog and semantic version are updated.

## Rollback

Prefer application rollback without a schema downgrade when the previous application
is compatible with the migrated schema. If not:

1. stop new writes;
2. capture a database backup;
3. review the exact Alembic downgrade SQL;
4. downgrade in a staging copy;
5. restore the previous application;
6. validate readiness and a repository round trip.

The initial migration downgrade deletes all PulseWatch tables and is therefore only
appropriate for disposable environments.

# Operations runbook

This runbook covers first-response diagnosis for a small PulseWatch deployment.
Commands assume the Docker Compose topology; adapt service names for another
orchestrator.

## First five checks

```bash
curl --fail http://127.0.0.1:8000/api/v1/health/live
curl --fail http://127.0.0.1:8000/api/v1/health/ready
docker compose ps
docker compose logs --tail=200 api
docker compose logs --tail=200 worker
```

Capture the time, deployment version, request ID, and affected service IDs before
changing state.

## API is unavailable

1. Check container/process state and restart count.
2. Check liveness directly, bypassing the reverse proxy when safe.
3. Inspect the last startup error.
4. Verify that Alembic completed.
5. Verify database DNS, port, credentials, and TLS settings.
6. Check CPU, memory, file descriptors, and connection limits.

Do not repeatedly restart a process that is failing a migration. Preserve the first
error and resolve schema/credentials deliberately.

## Liveness succeeds but readiness fails

Readiness requires a successful `SELECT 1` and every expected schema table.

```bash
docker compose exec api alembic current
docker compose exec postgres pg_isready -U pulsewatch -d pulsewatch
```

Check:

- database health and network path;
- `PULSEWATCH_DATABASE_URL`;
- expired/rotated credentials;
- connection saturation;
- migration version.

## Scheduler is not checking services

```bash
curl http://127.0.0.1:8000/api/v1/operations/scheduler
docker compose logs --tail=200 worker
```

Confirm:

- the worker is running;
- worker `PULSEWATCH_SCHEDULER_ENABLED=true`;
- `heartbeat_at` is recent;
- services are active;
- `next_check_at` is due;
- scheduler `last_error` is empty;
- the batch size and poll interval are sensible.

An authorized operator can request one cycle:

```bash
curl -X POST +  -H "X-API-Key: $PULSEWATCH_ADMIN_API_KEY" +  http://127.0.0.1:8000/api/v1/operations/scheduler/run
```

Do not run multiple worker replicas as a quick throughput fix.

## A target appears down

Inspect recent results and the open incident:

```bash
curl "http://127.0.0.1:8000/api/v1/checks?service_id=1&limit=10"
curl "http://127.0.0.1:8000/api/v1/incidents?service_id=1&status=open"
```

Differentiate:

- `BlockedTarget`: URL conflicts with outbound safety policy;
- `Timeout`: target did not complete within its configured timeout;
- `ConnectError`: DNS/TCP/TLS path failed;
- `UnexpectedStatus`: target answered but violated the configured contract.

Validate from the same network as the worker. A successful request from a laptop does
not prove the worker has the same DNS or egress route.

## Webhook notifications fail

```bash
curl +  -H "X-API-Key: $PULSEWATCH_ADMIN_API_KEY" +  "http://127.0.0.1:8000/api/v1/operations/notifications?limit=20"
```

Check final status, attempts, and error message. Then verify:

- receiver availability and TLS;
- target allowlist/egress;
- timeout and retry settings;
- shared signing secret;
- receiver idempotency behavior.

Do not disable signature verification to restore delivery. Fix secret rotation or
receiver configuration.

## High check latency or backlog

Review:

- `pulsewatch_check_latency_seconds`;
- due-service count and schedule intervals;
- timeout and retry configuration;
- scheduler batch size and concurrency;
- worker CPU/memory/network;
- upstream rate limits.

Increase concurrency gradually. High concurrency during an upstream outage can
amplify load because each check may use all retries.

## Database growth

Check that retention is running and `retention_last_run_at` advances. Trigger a
reviewed manual cycle if required:

```bash
curl -X POST +  -H "X-API-Key: $PULSEWATCH_ADMIN_API_KEY" +  http://127.0.0.1:8000/api/v1/operations/retention/run
```

Retention deletes check results only. Audit, incidents, and delivery history require
an explicit archival policy if their growth becomes material.

## Useful metrics

| Metric | Investigates |
| --- | --- |
| `pulsewatch_http_requests_total` | API traffic and status distribution |
| `pulsewatch_http_request_duration_seconds` | API latency |
| `pulsewatch_checks_total` | Healthy/failed checks by trigger |
| `pulsewatch_check_latency_seconds` | Target latency by service |
| `pulsewatch_incident_events_total` | Open/recovery transition rate |
| `pulsewatch_webhook_deliveries_total` | Delivered, failed, or blocked alerts |
| `pulsewatch_scheduler_cycles_total` | Worker cycle success/failure |
| `pulsewatch_scheduler_running` | In-process worker state |

## Recovery verification

After a change:

1. confirm liveness and readiness;
2. confirm the scheduler has a newer successful cycle;
3. run a controlled check;
4. verify result persistence and incident state;
5. verify metrics and logs include the request;
6. verify webhook delivery if an incident transition was involved;
7. record what changed and why in the incident timeline.

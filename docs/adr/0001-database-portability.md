# ADR 0001: SQLite locally, PostgreSQL in production

- Status: Accepted
- Date: 2026-08-27

## Context

A portfolio project should start without external infrastructure, while a
production-shaped API/worker topology requires a database designed for multiple
processes and concurrent writes.

## Decision

Use async SQLAlchemy as the persistence abstraction. Support:

- SQLite with `aiosqlite` for local execution and isolated tests;
- PostgreSQL with `asyncpg` for Docker Compose, CI integration, and production.

Use Alembic as the production schema authority. Keep repository queries compatible
with both databases and run a PostgreSQL migration/round-trip job in CI.

## Consequences

Positive:

- new contributors can run PulseWatch with only Python;
- the production topology can share durable state;
- application models and queries have one implementation;
- migrations are explicit and reviewable.

Negative:

- behavior common to both engines constrains some database-specific optimizations;
- SQLite cannot represent every PostgreSQL concurrency characteristic;
- CI needs a separate PostgreSQL integration job.

## Alternatives considered

- SQLite only: simpler, but weak for separate API/worker production topology.
- PostgreSQL only: operationally realistic, but adds friction to first run and unit
  tests.
- Raw SQL per engine: more control, but duplicates logic and increases maintenance
  for this project scale.

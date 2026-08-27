# Changelog

All notable changes are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [2.0.0] - 2026-08-27

### Added

- Per-service scheduled checks with a dedicated worker and persistent heartbeat.
- Incident lifecycle with open, repeated-failure, and resolved states.
- HMAC-SHA256 signed webhooks, idempotency keys, retries, and delivery history.
- Async SQLAlchemy repository supporting SQLite and PostgreSQL.
- Alembic migration history and PostgreSQL CI smoke test.
- Optional administrative API-key authentication.
- Mutation and operational audit history.
- Prometheus metrics for HTTP, checks, incidents, webhooks, and scheduler cycles.
- Structured JSON logging, request correlation, server timing, and security headers.
- Automated retention for expired check results.
- Versioned `/api/v1` contract and expanded operational endpoints.
- Production-shaped Docker Compose topology with PostgreSQL, API, and worker.
- CodeQL, dependency audit, Dependabot, release workflow, and pre-commit hooks.
- Architecture, deployment, webhook, security, ADR, and Spanish interview guides.

### Changed

- Rebuilt the dashboard for scheduled services, incidents, API-key entry, and
  operational controls.
- Upgraded the Docker image to a multi-stage, non-root runtime.
- Expanded tests to 101 cases and enforced at least 90% coverage.
- Preserved `/api/health` as a compatibility readiness endpoint.

### Security

- Added SSRF-oriented blocking for obvious private/reserved targets.
- Added constant-time administrative key comparison.
- Added explicit CORS allowlist configuration and restrictive browser headers.
- Added webhook authenticity and replay/deduplication support.

## [1.0.0] - 2026-08-27

### Added

- FastAPI service registration and on-demand checks.
- Async HTTP checks with bounded concurrency, timeouts, and selective retries.
- SQLite check history and reliability summary.
- Responsive dashboard, CLI, Docker image, CI, and initial test suite.

[2.0.0]: https://github.com/TheKhadaJhin/pulsewatch-api-monitor/releases/tag/v2.0.0
[1.0.0]: https://github.com/TheKhadaJhin/pulsewatch-api-monitor/releases/tag/v1.0.0

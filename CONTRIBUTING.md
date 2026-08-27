# Contributing to PulseWatch

Thank you for improving PulseWatch. Changes should preserve its core qualities:
explicit behavior, deterministic tests, secure defaults, and honest operational
boundaries.

## Development setup

```bash
git clone https://github.com/TheKhadaJhin/pulsewatch-api-monitor.git
cd pulsewatch-api-monitor
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
pre-commit install
```

Windows PowerShell activation:

```powershell
.venv\Scripts\Activate.ps1
```

## Before changing code

For a substantial feature, open an issue that describes:

- the user or operator problem;
- proposed behavior and API impact;
- data migration needs;
- security and failure cases;
- alternatives considered.

Report vulnerabilities privately according to [SECURITY.md](SECURITY.md).

## Local checks

```bash
ruff format .
ruff check .
mypy app
bandit -c pyproject.toml -r app
pytest --cov=app --cov-report=term-missing
```

Coverage must remain at or above 90%. New behavior should include success, failure,
validation, and transition tests where relevant. Tests must not rely on public
internet availability.

## Database changes

When changing persistence:

1. update SQLAlchemy models;
2. add an Alembic revision;
3. test upgrade from the previous revision;
4. test downgrade in a disposable database;
5. keep SQLite/PostgreSQL compatibility or document an intentional exception;
6. update the data model and deployment documentation.

Do not edit an already released migration to represent a new schema change.

## API changes

- Keep public routes under `/api/v1`.
- Use Pydantic models for contracts.
- Return explicit status codes and safe error details.
- Authenticate new mutations and sensitive operational reads.
- Update `docs/requests.http` and the README table.
- Treat incompatible contract changes as a new API version.

## Style

- Python 3.11 is the minimum supported version.
- Ruff owns formatting and import order.
- Public and internal functions use type annotations.
- Domain behavior belongs outside route handlers.
- Comments explain why, not syntax.
- Prefer small, testable changes over hidden magic.

## Commits and pull requests

Use focused commits with an imperative message, for example:

```text
feat: add incident acknowledgement endpoint
fix: prevent duplicate due-service selection
docs: explain webhook rotation
test: cover scheduler recovery after database error
```

A pull request should:

- explain the problem and solution;
- identify risks and tradeoffs;
- link the issue when one exists;
- include tests and migration notes;
- update documentation and changelog when user-visible;
- pass every CI and security check.

## Definition of done

- [ ] Behavior is tested without external network dependency.
- [ ] Formatting, lint, typing, security lint, and coverage pass.
- [ ] Database migrations are reversible in a disposable environment.
- [ ] Logs and errors do not expose secrets.
- [ ] Operational and security consequences are documented.
- [ ] README/API examples are current.
- [ ] Changelog is updated for a release-visible change.

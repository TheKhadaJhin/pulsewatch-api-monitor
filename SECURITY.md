# Security policy

## Supported versions

| Version | Security updates |
| --- | --- |
| 2.x | Yes |
| 1.x | No |

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Contact the repository
owner privately through the contact method on the GitHub profile:
[TheKhadaJhin](https://github.com/TheKhadaJhin).

Include:

- affected version or commit;
- reproduction steps or a minimal proof of concept;
- affected route/component;
- expected security impact;
- suggested mitigation, if known.

Do not access data you do not own, disrupt public services, persist after proving the
issue, or publish details before a fix is available.

## Threat model

PulseWatch accepts operator-controlled URLs and makes outbound requests. Its primary
risks are:

- server-side request forgery (SSRF);
- unauthorized configuration or manual checks;
- leaked credentials or webhook secrets;
- forged or replayed incident notifications;
- database exposure;
- denial of service through excessive schedules, concurrency, or response delays;
- operational-data disclosure through public reads, metrics, or logs.

## Implemented controls

### Administrative access

- Mutations and sensitive operational history support one optional administrative
  API key.
- Clients may send `X-API-Key` or `Authorization: Bearer`.
- Key comparison uses a constant-time function.
- The application never returns the configured key through its public config route.
- Mutation audit records actor and request ID.

Authentication is optional for a frictionless local demo. Set
`PULSEWATCH_ADMIN_API_KEY` in every shared environment.

### Outbound target safety

- Only `http` and `https` URLs are accepted.
- Embedded URL credentials are rejected.
- Localhost, `.local`, and `.internal` hostnames are blocked by default.
- Non-global IP literals—including loopback, private, link-local, and reserved
  addresses—are blocked by default.
- Private targets require an explicit configuration opt-in.
- Target redirects are not followed, preventing a public endpoint from redirecting a
  check into a private network.
- Implicit process-environment proxies are ignored; outbound routing changes must be
  deliberate at the network layer.
- Response bodies are neither downloaded nor persisted; checks use status and
  header-arrival latency only.

These checks do not fully prevent DNS rebinding or an initially public hostname from
resolving to a private address later. Production must enforce an outbound allowlist,
network policy, or controlled proxy.

### Webhooks

- Optional HMAC-SHA256 signatures authenticate the raw body.
- Idempotency keys identify an incident transition.
- Retries are limited and target only transient outcomes.
- Every final delivery outcome is stored.
- Webhook destinations pass the same outbound-target validation.

Consumers must verify signatures with constant-time comparison and deduplicate
idempotency keys. See [docs/WEBHOOKS.md](docs/WEBHOOKS.md).

### HTTP and application

- Pydantic validates types, ranges, methods, tags, and URLs.
- SQLAlchemy parameterizes database operations.
- Error messages avoid database details in HTTP responses.
- CSP, frame denial, MIME-sniffing prevention, permissions, and referrer headers are
  emitted.
- HSTS is emitted for HTTPS requests.
- CORS is disabled unless explicit origins are configured.
- Request IDs support investigation without exposing secrets.

### Runtime and supply chain

- The multi-stage image runs as a dedicated non-root user; Compose also drops Linux
  capabilities, prevents privilege escalation, and uses read-only API/worker roots.
- Dependencies use bounded compatible ranges.
- CI runs Ruff, mypy, tests, coverage, and Bandit.
- A scheduled workflow runs pip-audit.
- CodeQL and Dependabot are configured.
- PostgreSQL migrations are exercised in CI.

## Production hardening requirements

Before an internet-facing deployment:

1. configure a strong administrative API key through a secret manager;
2. terminate TLS at a trusted proxy/ingress;
3. restrict `/metrics`, audit, and notification history;
4. rate-limit mutation and manual-check routes;
5. enforce outbound network policy;
6. use a managed PostgreSQL identity with minimum permissions;
7. enable backups and test restoration;
8. centralize structured logs while redacting infrastructure-sensitive values;
9. set explicit CORS origins only when a separate frontend needs them;
10. run one scheduler worker unless a distributed lease is implemented.

Public service, incident, and summary reads are a deliberate status-page design.
Protect all routes at the reverse proxy if service names or URLs are confidential.

## Secret handling

Never commit:

- `PULSEWATCH_ADMIN_API_KEY`;
- `PULSEWATCH_WEBHOOK_SECRET`;
- production `PULSEWATCH_DATABASE_URL`;
- database backups;
- copied production configuration.

Rotate a secret immediately if it appears in a commit, build log, screenshot, or
support transcript. Removing it from the latest commit does not invalidate exposed
history.

## Security limitations

- No user accounts, OIDC, or role-based access control.
- No distributed rate limiter.
- No built-in secret rotation overlap for webhook signatures.
- No application-level encryption of database fields.
- No complete DNS-resolution pinning or rebinding defense.
- No tenant isolation.

These limits are acceptable for the documented single-operator/small-team scope, not
for an unmodified multi-tenant service.

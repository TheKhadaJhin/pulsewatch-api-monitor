# Incident webhook contract

PulseWatch can emit one webhook for each incident transition:

- `incident_opened`: first failed check while no incident is open;
- `incident_resolved`: first healthy check after an open incident.

Repeated failures update the same incident and do not emit repeated transition
events.

## Configuration

```bash
PULSEWATCH_WEBHOOK_URL=https://alerts.example.com/pulsewatch
PULSEWATCH_WEBHOOK_SECRET=<random-shared-secret>
PULSEWATCH_WEBHOOK_TIMEOUT_SECONDS=5
PULSEWATCH_WEBHOOK_RETRY_COUNT=2
```

The signing secret is optional in code but strongly recommended whenever the webhook
crosses a trust boundary.

## Request

```http
POST /pulsewatch HTTP/1.1
Content-Type: application/json
User-Agent: PulseWatch/2.0 (signed incident webhook)
X-PulseWatch-Event: incident_opened
Idempotency-Key: pulsewatch:42:incident_opened
X-PulseWatch-Signature: sha256=<hex-digest>
```

Example payload:

```json
{
  "event": "incident_opened",
  "incident": {
    "id": 42,
    "service_id": 7,
    "service_name": "Payments API",
    "status": "open",
    "opened_at": "2026-08-27T12:00:00Z",
    "resolved_at": null,
    "last_failure_at": "2026-08-27T12:00:00Z",
    "duration_seconds": null,
    "failure_count": 1,
    "initial_error_type": "UnexpectedStatus",
    "initial_error_message": "Expected HTTP 200, received 503",
    "last_error_type": "UnexpectedStatus",
    "last_error_message": "Expected HTTP 200, received 503"
  },
  "service": {
    "id": 7,
    "name": "Payments API",
    "description": "Checkout health endpoint",
    "url": "https://payments.example.com/health",
    "method": "GET",
    "expected_status": 200,
    "timeout_seconds": 5,
    "retry_count": 2,
    "check_interval_seconds": 60,
    "active": true,
    "tags": ["production", "payments"],
    "next_check_at": "2026-08-27T12:01:00Z",
    "created_at": "2026-08-20T09:00:00Z",
    "updated_at": "2026-08-20T09:00:00Z"
  }
}
```

Payload timestamps are serialized as ISO 8601 strings. Consumers should ignore
unknown fields so the contract can evolve compatibly.

## Signature verification

The signature is HMAC-SHA256 over the exact raw request body:

```python
import hashlib
import hmac


def verify_pulsewatch(body: bytes, signature_header: str, secret: str) -> bool:
    supplied = signature_header.removeprefix("sha256=")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied, expected)
```

Verify before parsing or acting on the payload. Do not re-serialize JSON before
verification because whitespace and key order would change the bytes.

## Idempotency

The idempotency key is:

```text
pulsewatch:{incident_id}:{event_type}
```

A receiver should store processed keys and return a successful `2xx` response for a
duplicate. This gives at-least-once delivery semantics without duplicate downstream
actions.

## Retry behavior

PulseWatch treats any `2xx` response as delivered.

| Outcome | Retry |
| --- | --- |
| `2xx` | No; success |
| `429` | Yes |
| `5xx` | Yes |
| Timeout/network error | Yes |
| Other `4xx` | No |

The number of attempts is `PULSEWATCH_WEBHOOK_RETRY_COUNT + 1`. Delay uses the
configured exponential backoff base. Final outcomes are saved in
`notification_deliveries` and available through
`GET /api/v1/operations/notifications`.

## Receiver recommendations

- Authenticate by HMAC, not source IP alone.
- Use constant-time signature comparison.
- Deduplicate with `Idempotency-Key`.
- Respond quickly, then queue slow downstream work.
- Return `2xx` only after durable acceptance.
- Log the event type, incident ID, and idempotency key.
- Alert on repeated signature failures.
- Rotate secrets through a controlled overlap procedure.
- Treat service URLs and error text as operational data.

## Testing locally

Private webhook destinations are blocked by default. For an isolated local demo only:

```bash
PULSEWATCH_ALLOW_PRIVATE_HOSTS=true
PULSEWATCH_WEBHOOK_URL=http://127.0.0.1:9000/events
```

Do not enable private targets on an internet-facing instance without strong egress
controls.

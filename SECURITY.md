# Security policy

## Reporting a vulnerability

Please report security issues privately to the repository owner instead of opening a
public issue. Include reproduction steps, affected endpoints and the expected impact.

## Relevant safeguards

- Only `http` and `https` targets are accepted.
- Target URLs containing credentials are rejected.
- Loopback, private, link-local and reserved IP literals are blocked by default.
- Common local hostnames are blocked unless private monitoring is explicitly enabled.
- Responses are not stored, preventing accidental collection of sensitive payloads.
- SQL statements are parameterized and API inputs are validated with Pydantic.
- The container runs as a non-root user.

`PULSEWATCH_ALLOW_PRIVATE_HOSTS=true` should only be used in a trusted environment.
For an internet-facing production deployment, add a strict outbound allowlist or proxy;
hostname checks alone are not a complete defense against DNS rebinding.

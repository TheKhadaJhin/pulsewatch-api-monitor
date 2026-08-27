# ADR 0002: Separate single scheduler worker

- Status: Accepted
- Date: 2026-08-27

## Context

Running an in-process scheduler inside every API replica would duplicate scheduled
checks and incident transitions. A fully distributed queue and lease system would
solve that problem but adds infrastructure and failure modes beyond the target scale.

## Decision

Support the scheduler in process for simple local development. In the
production-shaped topology:

- disable scheduling in API replicas;
- run `pulsewatch worker` as exactly one dedicated process;
- keep manual checks available through the API;
- use a per-process cycle lock to deduplicate overlapping local calls.

## Consequences

Positive:

- API replicas can scale independently;
- worker failure is visible and isolated from HTTP serving;
- deployment and explanation remain straightforward;
- no broker is required.

Negative:

- the worker is a scheduling single point of failure;
- multiple worker replicas are unsafe without coordination;
- throughput is limited to one process and its concurrency setting.

## Evolution trigger

Introduce a distributed lease or queue when any of these become true:

- scheduler high availability is required;
- one worker cannot complete due work within schedule tolerances;
- multiple regions need independent probes;
- scheduled work must survive process termination with durable acknowledgement.

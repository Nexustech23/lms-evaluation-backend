# LMS job recovery and deployment checks

The evaluation and paper-generation endpoints reuse an active submission. A
MongoDB guard survives API restarts; Redis stores the status and execution claim.
Do not clear a guard simply because a browser stopped polling.

## Before deployment

Use the verified checkout `/home/ubuntu/lms-evaluation-backend-new` and confirm its
branch, remote and commit. Deploy the API and worker from the same revision.
Drain old jobs before replacing workers: old code does not honor the new guards.

The existing default `QUEUE_MODE=inline` still runs work in the API process and
cannot survive an API restart. For work separated from API restarts, configure
`QUEUE_MODE=redis`, then start both services:

```sh
docker compose --profile queue up -d --build lms-backend-fastapi lms-worker
docker compose --profile queue ps
docker compose --profile queue logs --tail=100 lms-worker
```

Verify the external Redis service's persistence, availability and eviction policy
separately. The queue profile alone does not guarantee durability. Do not enable
automatic paid retries; the worker retains `max_tries=1`.

## Interrupted or missing status

An interrupted execution, ambiguous queue dispatch or unavailable Redis status
does not automatically start another billed job. A retained guard intentionally
blocks that behavior. An operator must first verify that the worker has stopped,
inspect the matching AI usage events and saved answer/paper result, and reconcile
any still-running provider call. Only then resolve the guard/status using the
organization's database administration procedure. No automatic guard-expiry or
bulk-delete recovery command is provided.

Evaluation status polling reconnects after temporary network/408/429/5xx errors.
The same browser tab can resume remembered evaluation job IDs after a refresh.
401/403/404 and explicit job failures stop checking; polling never submits a new
paid job. Job ownership is still checked on every status request.

## Verification limits

Local regression uses in-memory Mongo collections and fakeredis WATCH/MULTI
behavior, with provider and storage calls mocked. Production Redis/worker restart
tests, Redis persistence validation and live browser testing are deployment
checks, not evidence supplied by those tests. Multi-document exam deletion,
cross-document paper/version races and historical aggregate-weightage cleanup
remain separate work.

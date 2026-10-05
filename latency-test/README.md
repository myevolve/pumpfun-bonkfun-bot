# Latency probe (Cloud Run)

A read-only measurement service used to compare round-trip latency to the
bot's own infrastructure from GCP's network position, against the same probes
run from a local/mobile host.

It measures **transport reachability only**: RPC `getHealth` round trips,
a TCP connect to the Geyser endpoint, and a Jev API call. It does not
measure Geyser arrival, bank readiness, transaction inclusion, or
execution quality — a fast probe is not a fast fill.

## Configuration

The service reads exactly three environment variables:

| Variable | Meaning |
|---|---|
| `LAT_RPC_URL` | Full RPC endpoint including its key. A missing value makes `/probe` return HTTP 500 with `LAT_RPC_URL not configured`. |
| `LAT_GEYSER_HOST` | Geyser host and port. TCP connect only; no protocol. |
| `LAT_JEV_KEY` | API key for the Jev endpoint. |

`PORT` is honoured but Cloud Run sets it (default 8080).

## Deploy

```bash
# Run from this directory, or pass --source latency-test from the repo root.
gcloud run deploy latency-probe \
  --region us-central1 \
  --source . \
  --set-env-vars "LAT_RPC_URL=PLACEHOLDER,LAT_GEYSER_HOST=YOUR_HOST:PORT" \
  --set-secrets LAT_JEV_KEY=jev-api-key:latest \
  --no-allow-unauthenticated
```

Pass the Jev key through Secret Manager rather than inline in a shell
history. Nothing is logged or returned by the service. Deploy
unauthenticated only on a throwaway project: an open proxy to a paid RPC
endpoint is a bill, not a feature.

## Probe

```bash
curl -s "https://YOUR-SERVICE-URL/probe?n=20"
```

`n` is the sample count per target: **default 20**, clamped to 1..50, and
also the fallback when `n` is not a valid integer. Every sample is one
paid request, so the default costs 20 requests per configured target. The
response is JSON with per-target samples and summary statistics; `/`
behaves like `/probe?n=20`.

## Provenance

This is the tooling behind the sealed 2026-10-01 host-versus-GCP latency
comparison recorded in `docs/LESSONS.md` (and summarised in
`learning-examples/token-lifecycles/README.md`). It is kept so that study
can be repeated or re-checked; it is not wired into CI, the bot, or the
dashboard, and nothing in `src/` imports it.

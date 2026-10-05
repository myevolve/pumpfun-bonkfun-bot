# Latency probe (Cloud Run)

A read-only measurement service used to compare round-trip latency to the
bot's own infrastructure from GCP's network position, against the same probes
run from a local/mobile host.

It measures **transport reachability only**: RPC `getHealth` round trips,
a TCP connect to the Geyser endpoint, and a Jev API call. It does not
measure Geyser arrival, bank readiness, transaction inclusion, or
execution quality — a fast probe is not a fast fill.

## Deploy

```bash
gcloud run deploy latency-probe \
  --region us-central1 \
  --source . \
  --set-env-vars "SOLANA_NODE_RPC_ENDPOINT=${...}" \
  --no-allow-unauthenticated
```

Credentials are passed by environment variable and are never logged or
returned by the service. Deploy unauthenticated only on a throwaway
project: an open proxy to a paid RPC endpoint is a bill, not a feature.

## Probe

```bash
curl -s "https://<service-url>/probe?n=20"
```

`n` is the sample count per target (default 10). The response is JSON with
per-target samples and summary statistics.

## Provenance

This is the tooling behind the sealed 2026-10-01 host-versus-GCP latency
comparison recorded in `docs/LESSONS.md` (and summarised in
`learning-examples/token-lifecycles/README.md`). It is kept so that study
can be repeated or re-checked; it is not wired into CI, the bot, or the
dashboard, and nothing in `src/` imports it.

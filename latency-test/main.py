"""Latency probe service for Cloud Run.

Measures round-trip times to the bot's actual infrastructure from
GCP's network position (region chosen at deploy time), so they can be
compared against the same probes from the local/mobile host.

Endpoints:
    GET /probe?n=20   JSON with per-target latency samples + stats
    GET /             same as /probe?n=10

Credentials are passed via env (RPC endpoint incl. key, geyser endpoint,
Jev key) and never logged or returned.
"""

import json
import os
import socket
import statistics
import time
import urllib.error
import urllib.request

from flask import Flask, jsonify, request

app = Flask(__name__)

RPC_URL = os.environ.get("LAT_RPC_URL", "")
GEYSER_HOST = os.environ.get("LAT_GEYSER_HOST", "")  # host only, TCP connect
JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_KEY = os.environ.get("LAT_JEV_KEY", "")

# Health is the cheapest authenticated RPC; it exercises the full HTTPS path.
HEALTH_BODY = json.dumps(
    {"jsonrpc": "2.0", "id": 1, "method": "getHealth"}
).encode()


def _pctl(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    idx = min(int(len(vals) * p), len(vals) - 1)
    return vals[idx]


def _probe_rpc(n: int) -> list[float]:
    """JSON-RPC over HTTPS to the dedicated Chainstack node."""
    samples = []
    for _ in range(n):
        req = urllib.request.Request(  # noqa: S310 - fixed internal URL
            RPC_URL,
            data=HEALTH_BODY,
            headers={"Content-Type": "application/json"},
        )
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed internal URL
                req, timeout=5
            ) as resp:
                resp.read()
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
        samples.append((time.monotonic() - t0) * 1000)
    return samples


def _probe_geyser_tcp(n: int) -> list[float]:
    """TCP connect to the geyser endpoint (443) - measures network RTT to
    the geyser edge without gRPC machinery."""
    samples = []
    for _ in range(n):
        t0 = time.monotonic()
        try:
            with socket.create_connection(
                (GEYSER_HOST, 443), timeout=5
            ) as sock:
                sock.settimeout(5)
        except (OSError, TimeoutError):
            continue
        samples.append((time.monotonic() - t0) * 1000)
    return samples


def _probe_jev(n: int) -> list[float]:
    """System One judgment call - the model's own latency as seen from here."""
    if not JEV_KEY:
        return []
    body = json.dumps(
        {
            "state": {"symbol": "TEST", "buyers": 2, "real_sol": 0.3},
            "model": "jev-latest",
            "questions": {
                "quality": {
                    "type": "score",
                    "instructions": "Rate 0-4: state has symbol/buyers/real_sol.",
                    "criteria": ["dead", "weak", "neutral", "good", "great"],
                }
            },
        }
    ).encode()
    samples = []
    for _ in range(n):
        req = urllib.request.Request(  # noqa: S310 - fixed internal URL
            JEV_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {JEV_KEY}",
            },
        )
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed internal URL
                req, timeout=10
            ) as resp:
                resp.read()
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
        samples.append((time.monotonic() - t0) * 1000)
    return samples


def _summarize(name: str, samples: list[float]) -> dict:
    if not samples:
        return {"target": name, "samples": 0, "error": "all probes failed"}
    return {
        "target": name,
        "samples": len(samples),
        "p50_ms": round(_pctl(samples, 0.5)),
        "p95_ms": round(_pctl(samples, 0.95)),
        "mean_ms": round(statistics.mean(samples)),
        "stdev_ms": round(statistics.stdev(samples)) if len(samples) > 1 else 0,
        "min_ms": round(min(samples)),
        "max_ms": round(max(samples)),
    }


@app.route("/probe")
def probe() -> object:
    try:
        n = max(1, min(50, int(request.args.get("n", "20"))))
    except ValueError:
        n = 20
    if not RPC_URL:
        return jsonify({"error": "LAT_RPC_URL not configured"}), 500
    results = {
        "rpc_chainstack": _summarize("rpc_chainstack", _probe_rpc(n)),
        "geyser_tcp": (
            _summarize("geyser_tcp", _probe_geyser_tcp(n))
            if GEYSER_HOST
            else {"target": "geyser_tcp", "samples": 0, "error": "not configured"}
        ),
        "jev_api": (
            _summarize("jev_api", _probe_jev(n))
            if JEV_KEY
            else {"target": "jev_api", "samples": 0, "error": "not configured"}
        ),
        "meta": {"n_requested": n, "ts_utc": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )},
    }
    return jsonify(results)


@app.route("/")
def index() -> object:
    return probe()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))  # noqa: S104 - Cloud Run requirement

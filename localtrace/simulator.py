"""Simulated instrumented services — local functions/processes only.

``simulate_trace`` builds a realistic multi-service trace (HTTP -> service
-> db/cache, with parallel fan-out and occasional errors) entirely in
memory, deliberately in a *shuffled* arrival order to exercise the
out-of-order ingestion path.

``run_simulated_process`` performs the same work from a separate OS process
(``python -m localtrace.simulator ...``) that POSTs spans to a local server;
this proves a real process boundary with zero external services.
"""

from __future__ import annotations

import argparse
import json
import random
from typing import Any

from .model import Span, Status


def simulate_trace(trace_id: str = "t-demo", *, base_us: int = 1_000_000,
                   seed: int | None = 7,
                   force_error: bool = False) -> list[Span]:
    """Generate one deterministic demo trace; spans returned shuffled."""
    rng = random.Random(seed)

    def span(span_id: str, parent_id: str, service: str, name: str,
             start: int, duration: int, status: Status = Status.OK,
             **attrs: Any) -> Span:
        return Span(
            trace_id=trace_id, span_id=span_id, parent_id=parent_id,
            service=service, name=name, start_us=start,
            end_us=start + duration, status=status,
            attributes=dict(attrs),
        )

    spans = [
        span("s1", "", "gateway", "GET /checkout", base_us, 600,
             http_method="GET", http_route="/checkout"),
        span("s2", "s1", "auth", "verify_token", base_us + 20, 60,
             component="jwt"),
        span("s3", "s1", "checkout", "create_order", base_us + 90, 420,
             order_id=42),
        span("s4", "s3", "inventory", "reserve_items", base_us + 110, 120,
             db_system="sqlite"),
        # Two parallel children of s3.
        span("s5", "s3", "payment", "charge_card", base_us + 250, 200,
             provider="stub-pay"),
        span("s6", "s3", "notify", "send_email", base_us + 260, 140,
             channel="email"),
        span("s7", "s5", "payment-db", "update_ledger", base_us + 280, 90,
             db_system="sqlite"),
    ]
    if force_error or (seed is not None and seed % 3 == 0 and not force_error):
        bad = next(s for s in spans if s.span_id == "s7")
        idx = spans.index(bad)
        spans[idx] = Span(
            trace_id=bad.trace_id, span_id=bad.span_id, parent_id=bad.parent_id,
            service=bad.service, name=bad.name, start_us=bad.start_us,
            end_us=bad.end_us, status=Status.ERROR,
            attributes={**dict(bad.attributes), "error": "ledger locked"},
        )

    rng.shuffle(spans)
    return spans


def run_simulated_process(url: str, trace_id: str = "t-proc",
                          seed: int = 11) -> dict[str, Any]:
        """Run as a local *process*: generate spans and POST them over HTTP."""
        from .server import post_spans

        spans = simulate_trace(trace_id, base_us=2_000_000, seed=seed)
        return post_spans(url, spans)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m localtrace.simulator",
        description="Simulated local service process; emits spans over HTTP.",
    )
    parser.add_argument("--url", default="http://127.0.0.1:8765/spans")
    parser.add_argument("--trace-id", default="t-proc")
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--print", action="store_true",
                        help="print spans as JSON to stdout instead of POSTing")
    args = parser.parse_args(argv)

    if args.print:
        spans = simulate_trace(args.trace_id, seed=args.seed)
        print(json.dumps([s.to_dict() for s in spans], indent=2))
        return 0

    result = run_simulated_process(args.url, args.trace_id, args.seed)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

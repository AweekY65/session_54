"""Local "services" used to exercise the collector.

These are *not* real microservices: they are ordinary local functions that
emit :class:`~tracelet.model.Span` objects, mimicking a multi-service call
(edge -> checkout -> payment/inventory -> postgres) with parallel branches,
out-of-order reporting, an occasional error and an orphan/duplicate when
requested. Nothing is sent over the network.
"""

from __future__ import annotations

import random
from typing import Optional

from .model import Span

# Fixed base time so generated reports are deterministic.
T0 = 1_700_000_000_000_000  # arbitrary microsecond timestamp


def _span(trace_id: str, span_id: str, parent: Optional[str], service: str,
          name: str, start: int, dur: int, status: str = "OK",
          **attrs) -> Span:
    return Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_id=parent,
        service=service,
        name=name,
        start_us=start,
        end_us=start + dur,
        status=status,
        attributes=dict(attrs),
    )


def simulate_checkout(trace_id: str = "tr-demo",
                      *, force_error: bool = False,
                      inject_orphan: bool = False) -> list[Span]:
    """Return spans for one checkout trace (logical time order).

    Timeline (us, children nested in parent)::

      edge /checkout 0..300
        checkout svc  10..290
          inventory  30..120   (parallel)
          payment    30..200
            postgres 50..150
          notify    210..280   (parallel tail)
    """
    t = T0
    spans = [
        _span(trace_id, "edge", None, "edge", "GET /checkout",
              t + 0, 300, http_method="GET", http_route="/checkout"),
        _span(trace_id, "checkout", "edge", "checkout", "Checkout",
              t + 10, 280),
        _span(trace_id, "inventory", "checkout", "inventory", "ReserveStock",
              t + 30, 90, region="cn-north"),
        _span(trace_id, "payment", "checkout", "payment", "Charge",
              t + 30, 170, status="ERROR" if force_error else "OK",
              amount=4200, currency="CNY"),
        _span(trace_id, "db", "payment", "postgres", "UPDATE payments",
              t + 50, 100, db_system="postgresql"),
        _span(trace_id, "notify", "checkout", "notify", "SendReceipt",
              t + 210, 70),
    ]
    if inject_orphan:
        spans.append(_span(trace_id, "ghost", "missing-parent",
                           "billing", "Reconcile", t + 0, 50))
    return spans


def simulate_traces(n: int = 3, *, seed: int = 7) -> list[Span]:
    """Generate ``n`` independent traces; reporting order is shuffled."""
    rng = random.Random(seed)
    all_spans: list[Span] = []
    for i in range(n):
        tid = f"tr-{i:03d}"
        spans = simulate_checkout(
            tid,
            force_error=(i == 1),
            inject_orphan=(i == n - 1),
        )
        # Slide each trace forward so retention ordering is meaningful.
        slide = i * 1_000_000
        spans = [Span(**{**sp.to_dict(),
                         "trace_id": tid,
                         "start_us": sp.start_us + slide,
                         "end_us": sp.end_us + slide})
                 for sp in spans]
        rng.shuffle(spans)  # children often arrive before parents
        all_spans.extend(spans)
    return all_spans


def spans_out_of_order(trace_id: str = "tr-ooo") -> list[Span]:
    """A tiny trace deliberately returned deepest-span-first."""
    ordered = simulate_checkout(trace_id)
    # Reverse logical order: leaves/children before root.
    return list(reversed(ordered))

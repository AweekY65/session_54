"""Shared test fixtures and span builders."""

import pytest

from localtrace.model import Span, Status


@pytest.fixture
def simple_spans():
    """A normal 4-span tree:

        root (svc-a, 0..100)
        ├── c1 (svc-b, 10..80)
        │   └── gc (svc-c, 20..60)
        └── c2 (svc-b, 85..95)
    """
    return [
        Span("t1", "root", "", "svc-a", 0, 100, status=Status.OK, name="op"),
        Span("t1", "c1", "root", "svc-b", 10, 80, status=Status.OK,
             name="op1"),
        Span("t1", "gc", "c1", "svc-c", 20, 60, status=Status.OK,
             name="op2"),
        Span("t1", "c2", "root", "svc-b", 85, 95, status=Status.OK,
             name="op3"),
    ]


def make_span(trace_id="t", span_id="s", parent_id="", service="svc",
              start=0, end=10, status=Status.OK, **attrs):
    return Span(trace_id=trace_id, span_id=span_id, parent_id=parent_id,
                service=service, start_us=start, end_us=end,
                status=status, name="op", attributes=dict(attrs))

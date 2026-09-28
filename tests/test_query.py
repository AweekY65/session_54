"""Filtering by service, status, attribute and time range."""

import pytest

from conftest import make_span
from localtrace.model import Status
from localtrace.query import TraceFilter, query_traces
from localtrace.store import SpanStore


@pytest.fixture
def populated(tmp_path):
    store = SpanStore(str(tmp_path / "q"))
    # t1: svc-a+svc-b, OK, spans 0..100; attr env=dev, route=/x
    store.add_spans([
        make_span(trace_id="t1", span_id="r", service="svc-a", start=0,
                  end=100, env="dev", route="/x"),
        make_span(trace_id="t1", span_id="c", parent_id="r", service="svc-b",
                  start=10, end=90, env="dev"),
    ])
    # t2: svc-a only, ERROR (one span), 200..300; env=prod
    store.add_spans([
        make_span(trace_id="t2", span_id="r", service="svc-a", start=200,
                  end=300, status=Status.ERROR, env="prod"),
    ])
    # t3: svc-c, UNSET, 400..420; attr retry=3, tags=["a","b"]
    store.add_spans([
        make_span(trace_id="t3", span_id="r", service="svc-c", start=400,
                  end=420, status=Status.UNSET, retry=3, tags=["a", "b"]),
    ])
    return store


def ids(results):
    return [a.trace_id for a in results]


def test_filter_by_service_and(populated):
    assert ids(query_traces(populated, TraceFilter(services=["svc-a"]))) == [
        "t1", "t2"]
    assert ids(query_traces(populated,
                            TraceFilter(services=["svc-a", "svc-b"]))) == ["t1"]
    assert query_traces(populated, TraceFilter(services=["nope"])) == []


def test_filter_by_status(populated):
    assert ids(query_traces(
        populated, TraceFilter(statuses=[Status.ERROR]))) == ["t2"]
    assert ids(query_traces(
        populated, TraceFilter(statuses=[Status.OK]))) == ["t1"]
    # OR within statuses
    assert ids(query_traces(
        populated, TraceFilter(statuses=[Status.ERROR, Status.UNSET]))) == [
        "t2", "t3"]


def test_filter_by_attribute_equality_and_key(populated):
    attrs, keys = TraceFilter.parse_attributes(["env=dev"])
    assert ids(query_traces(populated,
                            TraceFilter(attributes=attrs))) == ["t1"]
    attrs, keys = TraceFilter.parse_attributes(["env"])
    key_only = query_traces(
        populated, TraceFilter(attributes=attrs,
                               attribute_keys_only=keys))
    assert ids(key_only) == ["t1", "t2"]
    attrs, _ = TraceFilter.parse_attributes(["retry=3"])
    assert ids(query_traces(populated, TraceFilter(attributes=attrs))) == ["t3"]
    # membership inside a list attribute
    attrs, _ = TraceFilter.parse_attributes(["tags=b"])
    assert ids(query_traces(populated, TraceFilter(attributes=attrs))) == ["t3"]
    # json typed value
    attrs, _ = TraceFilter.parse_attributes(["tags:=json:3"])
    assert query_traces(populated, TraceFilter(attributes=attrs)) == []
    attrs, _ = TraceFilter.parse_attributes(["retry:=json:3"])
    assert ids(query_traces(populated, TraceFilter(attributes=attrs))) == ["t3"]


def test_filter_by_time_overlap(populated):
    # window ending before t2 starts -> only t1
    f = query_traces(populated, TraceFilter(end_us=150))
    assert ids(f) == ["t1"]
    # window starting after t1 ends
    f = query_traces(populated, TraceFilter(start_us=150))
    assert ids(f) == ["t2", "t3"]
    # narrow window intersecting only t2
    f = query_traces(populated, TraceFilter(start_us=250, end_us=260))
    assert ids(f) == ["t2"]
    # combined with service
    f = query_traces(populated, TraceFilter(
        services=["svc-a"], start_us=150))
    assert ids(f) == ["t2"]


def test_filter_trace_ids_and_limit(populated):
    f = query_traces(populated, TraceFilter(trace_ids=["t3"]))
    assert ids(f) == ["t3"]
    assert len(query_traces(populated, TraceFilter(), limit=2)) == 2


def test_result_order_is_stable(populated):
    first = ids(query_traces(populated, TraceFilter()))
    second = ids(query_traces(populated, TraceFilter()))
    assert first == second == ["t1", "t2", "t3"]

"""Model: span construction, JSON import, timestamp handling."""

import pytest

from localtrace.model import Span, Status, micros_to_iso, to_micros


def test_from_dict_full():
    span = Span.from_dict({
        "trace_id": "t", "span_id": "a", "parent_id": "b",
        "service": "svc", "name": "do",
        "start": "2026-09-28T10:00:00Z",
        "end": "2026-09-28T10:00:00.001Z",
        "status": "error", "attributes": {"x": 1},
    })
    assert span.duration_us == 1000
    assert span.status is Status.ERROR
    assert span.attributes == {"x": 1}
    assert span.parent_id == "b"


def test_from_dict_duration_aliases():
    s1 = Span.from_dict({"trace_id": "t", "span_id": "a", "service": "s",
                         "start_us": 10, "duration_us": 5})
    assert s1.end_us == 15
    s2 = Span.from_dict({"trace_id": "t", "span_id": "b", "service": "s",
                         "start_us": 10, "duration_ms": 2})
    assert s2.end_us == 2010


def test_to_micros_and_back():
    us = to_micros("2026-09-28T10:00:00.000001Z")
    assert micros_to_iso(us) == "2026-09-28T10:00:00.000001Z"
    assert to_micros(123) == 123
    assert to_micros(123.9) == 123


def test_root_parent_forms():
    for pid in ("", "0", "0000000000000000"):
        s = Span("t", "s", pid, "svc", 0, 1)
        assert s.is_root


def test_validation():
    with pytest.raises(ValueError):
        Span("t", "s", "", "svc", 10, 5)  # end before start
    with pytest.raises(ValueError):
        Span.from_dict({"trace_id": "t", "span_id": "s",
                        "service": "svc", "start_us": 1})  # no end/duration
    with pytest.raises(ValueError):
        Span("", "s", "", "svc", 0, 1)


def test_to_dict_stable_order():
    s = Span("t", "s", "", "svc", 0, 10, Status.OK, {"b": 2, "a": 1}, "n")
    d1 = s.to_dict()
    d2 = s.to_dict()
    assert list(d1) == list(d2)
    assert list(d1["attributes"]) == ["a", "b"]
    assert d1["duration_us"] == 10

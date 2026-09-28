"""Trace filtering / querying against reconstructed traces.

Filters work on the trace-level summary and every span in the trace:

* ``service``  : trace contains a span from this service
* ``status``   : trace status / a span status ("OK" or "ERROR")
* ``attr``     : some span carries attribute ``key`` (optionally == value)
* time range   : the trace time window *overlaps* [start_us, end_us]

Numeric attribute values compare numerically when both the stored value and
the filter value are numbers; otherwise they compare as normalized strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Union

from .analyzer import Trace
from .model import parse_time_us

TraceLike = Union[Trace, dict]


@dataclass
class Filter:
    service: Optional[str] = None
    status: Optional[str] = None
    attr_key: Optional[str] = None
    attr_value: Optional[str] = None
    start_us: Optional[int] = None
    end_us: Optional[int] = None

    def matches(self, trace: TraceLike) -> bool:
        td = trace.to_dict() if isinstance(trace, Trace) else trace
        spans = _spans_of(td)

        if self.service is not None:
            if not any(s["service"] == self.service for s in spans):
                return False

        if self.status is not None:
            # Trace-level status: ERROR if any span errored, else OK.
            if td.get("status") != self.status:
                return False

        if self.start_us is not None and td["end_us"] < self.start_us:
            return False
        if self.end_us is not None and td["start_us"] > self.end_us:
            return False

        if self.attr_key is not None:
            def attr_ok(s: dict) -> bool:
                attrs = s.get("attributes") or {}
                if self.attr_key not in attrs:
                    return False
                if self.attr_value is None:
                    return True
                return _attr_equal(attrs[self.attr_key], self.attr_value)

            if not any(attr_ok(s) for s in spans):
                return False
        return True


def _attr_equal(actual: Any, wanted: str) -> bool:
    if isinstance(actual, bool):
        return str(actual).lower() == wanted.lower()
    if isinstance(actual, (int, float)):
        try:
            return float(actual) == float(wanted)
        except ValueError:
            return str(actual) == wanted
    return str(actual) == wanted


def _spans_of(td: dict) -> list[dict]:
    if "spans" in td:  # already flattened (shouldn't happen, but tolerate it)
        return td["spans"]
    out: list[dict] = []

    def walk(node: dict) -> None:
        out.append(node["span"])
        for ch in node.get("children", []):
            walk(ch)

    for root in td.get("roots", []):
        walk(root)
    return out


def list_traces(
    traces: Iterable[TraceLike],
    flt: Optional[Filter] = None,
    limit: Optional[int] = None,
) -> list[TraceLike]:
    """Return matching traces, stably ordered by (start, trace_id)."""
    out = [t for t in traces if flt is None or flt.matches(t)]

    def key(t: TraceLike) -> tuple[int, str]:
        td = t.to_dict() if isinstance(t, Trace) else t
        return (td["start_us"], td["trace_id"])

    out.sort(key=key)
    if limit is not None:
        out = out[:limit]
    return out


def make_filter(
    service: Optional[str] = None,
    status: Optional[str] = None,
    attr: Optional[str] = None,
    start: Any = None,
    end: Any = None,
) -> Filter:
    """Build a Filter from CLI-ish inputs.

    ``attr`` is "key" or "key=value"; ``start``/``end`` accept anything
    :func:`tracelet.model.parse_time_us` understands.
    """
    attr_key = attr_value = None
    if attr:
        if "=" in attr:
            attr_key, attr_value = attr.split("=", 1)
            attr_key = attr_key.strip()
            attr_value = attr_value.strip()
        else:
            attr_key = attr.strip()
    status = status.upper() if status else None
    return Filter(
        service=service,
        status=status,
        attr_key=attr_key,
        attr_value=attr_value,
        start_us=parse_time_us(start) if start is not None else None,
        end_us=parse_time_us(end) if end is not None else None,
    )

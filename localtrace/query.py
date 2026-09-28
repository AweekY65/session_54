"""Trace filtering / querying.

Filters compose with AND. A trace matches when:

* **service** — every listed service occurs in the trace (AND within the
  list); use separate queries for OR semantics.
* **status** — the trace's rolled-up status equals one of the listed values
  (OR within the list; ``ERROR`` means at least one span errored).
* **attribute** — every ``key=value`` pair is present on *some* span of the
  trace. A bare ``key`` checks existence; ``value`` is compared both as a
  string and structurally (numbers/bools/list membership).
* **time range** — the trace's [start, end] overlaps [start_us, end_us].
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .analysis import TraceAnalysis
from .model import Status
from .store import SpanStore


@dataclass
class TraceFilter:
    services: list[str] = field(default_factory=list)
    statuses: list[Status] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    attribute_keys_only: set[str] = field(default_factory=set)
    start_us: int | None = None
    end_us: int | None = None
    trace_ids: list[str] | None = None

    @classmethod
    def parse_attributes(cls, pairs: list[str] | dict[str, Any] | None
                         ) -> tuple[dict[str, Any], set[str]]:
        """Parse ``key`` / ``key=value`` / ``key:=json`` filter specs."""
        attrs: dict[str, Any] = {}
        keys_only: set[str] = set()
        if pairs is None:
            return attrs, keys_only
        if isinstance(pairs, dict):
            for k, v in pairs.items():
                if v is None:
                    keys_only.add(k)
                else:
                    attrs[k] = v
            return attrs, keys_only
        for spec in pairs:
            if ":=" in spec:
                key, raw = spec.split(":=", 1)
                key = key.strip()
                raw = raw.strip()
                if raw.startswith("json:"):
                    raw = raw[len("json:"):]
                attrs[key] = json.loads(raw)
            elif "=" in spec:
                key, raw = spec.split("=", 1)
                key = key.strip()
                raw = raw.strip()
                if raw.startswith("json:"):
                    attrs[key] = json.loads(raw[len("json:"):])
                else:
                    attrs[key] = _coerce_scalar(raw)
            else:
                keys_only.add(spec.strip())
        return attrs, keys_only

    def to_dict(self) -> dict[str, Any]:
        return {
            "services": list(self.services),
            "statuses": [s.value if isinstance(s, Status) else s
                         for s in self.statuses],
            "attributes": dict(sorted(self.attributes.items())),
            "attribute_keys_only": sorted(self.attribute_keys_only),
            "start_us": self.start_us,
            "end_us": self.end_us,
            "trace_ids": self.trace_ids,
        }


def _coerce_scalar(raw: str) -> Any:
    if raw in ("true", "True"):
        return True
    if raw in ("false", "False"):
        return False
    if raw in ("null", "None"):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _attr_matches(expected: Any, actual: Any) -> bool:
    if actual == expected:
        return True
    # Stringified comparison covers "2" vs 2 coming from JSON/CLI.
    if isinstance(actual, str) and str(expected) == actual:
        return True
    if isinstance(actual, (list, tuple)):
        return any(_attr_matches(expected, item) for item in actual)
    if isinstance(actual, dict):
        return any(_attr_matches(expected, v) for v in actual.values())
    return False


def matches(analysis: TraceAnalysis, flt: TraceFilter) -> bool:
    tree = analysis.tree

    if flt.trace_ids is not None and analysis.trace_id not in flt.trace_ids:
        return False

    if flt.services:
        present = {s.service for s in tree.spans.values()}
        if any(svc not in present for svc in flt.services):
            return False

    if flt.statuses:
        wanted = {Status.coerce(s) for s in flt.statuses}
        if analysis.status not in wanted:
            return False

    if flt.attributes or flt.attribute_keys_only:
        collected: dict[str, list[Any]] = {}
        for span in tree.spans.values():
            for key, value in span.attributes.items():
                collected.setdefault(key, []).append(value)
        for key in flt.attribute_keys_only:
            if key not in collected:
                return False
        for key, expected in flt.attributes.items():
            values = collected.get(key)
            if values is None:
                return False
            if not any(_attr_matches(expected, v) for v in values):
                return False

    if flt.start_us is not None and analysis.end_us < flt.start_us:
        return False
    if flt.end_us is not None and analysis.start_us > flt.end_us:
        return False

    return True


def query_traces(store: SpanStore, flt: TraceFilter | None = None,
                 *, limit: int | None = None) -> list[TraceAnalysis]:
    """Return matching analyses in a stable order (start, duration, id)."""
    flt = flt or TraceFilter()
    out = [a for a in store.analyses() if matches(a, flt)]
    out.sort(key=lambda a: (a.start_us, a.duration_us, a.trace_id))
    if limit is not None:
        out = out[:limit]
    return out

"""Span data model.

A span describes one unit of work in one service:

    trace_id   : id of the trace the span belongs to
    span_id    : unique id of this span within the trace
    parent_id  : id of the parent span, or None/"" for a root span
    service    : service / process name
    name       : operation name (e.g. HTTP route or RPC method)
    start_us   : start time, microseconds since the Unix epoch
    end_us     : end time,   microseconds since the Unix epoch
    status     : "OK" or "ERROR"
    attributes: flat string -> primitive mapping (str/int/float/bool)

Time is always normalised to integer microseconds. Strings that look like
integers ("1700000000000000") are accepted as microseconds; ISO-8601 strings
("2026-09-28T10:00:00Z") are accepted for human-authored imports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

_OK = "OK"
_ERROR = "ERROR"
_STATUS_ALIASES = {
    "": _OK,
    "OK": _OK,
    "OK_CODE": _OK,
    "SUCCESS": _OK,
    "UNSET": _OK,
    "ERROR": _ERROR,
    "ERR": _ERROR,
    "FAIL": _ERROR,
    "FAILED": _ERROR,
}


class ModelError(ValueError):
    """Raised when a span payload cannot be turned into a valid Span."""


_INT_RE = re.compile(r"^-?\d+$")


def parse_time_us(value: Any) -> int:
    """Normalise a timestamp to integer microseconds since the epoch.

    Accepted forms:
      * int / float                 -> already microseconds
      * "<digits>"                  -> microseconds as a string
      * ISO-8601 string with 'T'    -> parsed via datetime.fromisoformat
                                       ('Z' suffix supported, py>=3.11 too)
    """
    if value is None:
        raise ModelError("timestamp is required")
    if isinstance(value, bool):
        raise ModelError(f"invalid timestamp: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if _INT_RE.match(s):
            return int(s)
        iso = s[:-1] + "+00:00" if s.endswith("Z") else s
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError as exc:
            raise ModelError(f"unparseable timestamp: {value!r}") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1_000_000)
    raise ModelError(f"unsupported timestamp type: {type(value).__name__}")


def normalize_status(value: Any) -> str:
    if value is None:
        return _OK
    s = str(value).strip().upper().replace("STATUS_CODE_", "")
    if s in _STATUS_ALIASES:
        return _STATUS_ALIASES[s]
    # OTLP numeric codes: 0 = unset/ok, 1 = ok, 2 = error
    if s == "0" or s == "1":
        return _OK
    if s == "2":
        return _ERROR
    raise ModelError(f"invalid status: {value!r}")


def _normalize_attributes(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ModelError("attributes must be an object")
    out: dict[str, Any] = {}
    for key, val in raw.items():
        if not isinstance(key, str):
            raise ModelError("attribute keys must be strings")
        if isinstance(val, bool) or val is None:
            out[key] = val if val is not None else None
        elif isinstance(val, (str, int, float)):
            out[key] = val
        else:
            # Lists / nested objects: keep a stable JSON-ish string form so
            # equality filtering still works deterministically.
            out[key] = str(val)
    return out


@dataclass(frozen=True)
class Span:
    """One reported span. Immutable and hashable by (trace_id, span_id)."""

    trace_id: str
    span_id: str
    parent_id: Optional[str]
    service: str
    name: str
    start_us: int
    end_us: int
    status: str = _OK
    attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        tr = (self.trace_id or "").strip()
        sp = (self.span_id or "").strip()
        if not tr:
            raise ModelError("trace_id is required")
        if not sp:
            raise ModelError("span_id is required")
        parent = self.parent_id.strip() if isinstance(self.parent_id, str) else None
        if parent == "":
            parent = None
        if not self.service or not str(self.service).strip():
            raise ModelError("service is required")
        object.__setattr__(self, "trace_id", tr)
        object.__setattr__(self, "span_id", sp)
        object.__setattr__(self, "parent_id", parent)
        object.__setattr__(self, "service", str(self.service).strip())
        object.__setattr__(self, "name", str(self.name or "").strip())
        if self.end_us < self.start_us:
            raise ModelError(
                f"span {sp}: end_us ({self.end_us}) before start_us ({self.start_us})"
            )

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us

    def is_error(self) -> bool:
        return self.status == _ERROR

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d["parent_id"] is None:
            d["parent_id"] = None
        return d

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Span":
        if not isinstance(raw, Mapping):
            raise ModelError("span record must be an object")

        def pick(*names: str) -> Any:
            for n in names:
                if n in raw and raw[n] is not None:
                    return raw[n]
            return None

        trace_id = pick("trace_id", "traceId", "traceID")
        span_id = pick("span_id", "spanId", "spanID", "id")
        # parent may legitimately be absent / null / ""
        if "parent_id" in raw:
            parent = raw["parent_id"]
        elif "parentId" in raw:
            parent = raw["parentId"]
        elif "parentID" in raw:
            parent = raw["parentID"]
        elif "parent_span_id" in raw:
            parent = raw["parent_span_id"]
        else:
            parent = None
        service = pick("service", "service_name", "serviceName")
        name = pick("name", "operation", "operation_name", "span_name") or ""
        start = pick("start_us", "startUs", "start_time_us", "startTimeUs",
                     "start_time", "startTime", "start")
        end = pick("end_us", "endUs", "end_time_us", "endTimeUs",
                   "end_time", "endTime", "end")
        status = raw.get("status", raw.get("status_code", raw.get("statusCode")))
        attrs = raw.get("attributes", raw.get("tags", {}))

        return cls(
            trace_id=str(trace_id) if trace_id is not None else "",
            span_id=str(span_id) if span_id is not None else "",
            parent_id=str(parent) if parent is not None else None,
            service=str(service) if service is not None else "",
            name=str(name),
            start_us=parse_time_us(start),
            end_us=parse_time_us(end),
            status=normalize_status(status),
            attributes=_normalize_attributes(attrs),
        )

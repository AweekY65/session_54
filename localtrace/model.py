"""Span data model.

A span is the unit of tracing work captured in the system.

Fields
------
trace_id : str
    Identifies the whole distributed trace; spans sharing a trace_id form
    one trace.
span_id : str
    Unique *within a trace*. The same span_id appearing twice in one trace is
    a duplicate and is reported by tree reconstruction.
parent_id : str | None
    span_id of the direct parent. ``None`` / empty string marks a root.
service, name : str
    Instrumented service name and the operation/span name.
start_ns : int
    Start timestamp in nanoseconds since the Unix epoch. Import helpers also
    accept ``start_ms`` / ISO-8601 strings / datetime objects.
duration_ns : int
    Span duration in nanoseconds (``>= 0``); ``end_ns = start_ns + duration_ns``.
status : str
    One of ``OK`` / ``ERROR`` / ``UNSET``.
attributes : dict[str, Any]
    Free-form key/value tags. Keys must be strings; values must be JSON
    serializable (they are persisted to JSONL files).

Wire/file example (one JSON object per line)::

    {"trace_id":"t1","span_id":"s1","parent_id":null,"service":"gateway",
     "name":"GET /checkout","start_ns":1000,"duration_ns":1000,
     "status":"OK","attributes":{"http.method":"GET"}}
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any

VALID_STATUSES = ("OK", "ERROR", "UNSET")
_ID_RE = re.compile(r"^[0-9a-zA-Z._\-]+$")


class SpanError(ValueError):
    """Raised when a span payload cannot be turned into a valid :class:`Span`."""


def _parse_ts_ns(value: Any, field_name: str) -> int:
    """Parse a timestamp expressed in ns/ms (int/float) or ISO-8601 text."""
    if isinstance(value, bool):  # bool is an int subclass — reject it explicitly
        raise SpanError(f"{field_name} must be a timestamp, got bool")
    if isinstance(value, int):
        if value < 0:
            raise SpanError(f"{field_name} must be non-negative, got {value}")
        return value
    if isinstance(value, float):
        if value < 0:
            raise SpanError(f"{field_name} must be non-negative, got {value}")
        return int(value)
    if isinstance(value, _dt.datetime):
        return int(value.timestamp() * 1_000_000_000)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
        try:
            dt = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SpanError(f"invalid {field_name}: {value!r}") from exc
        return int(dt.timestamp() * 1_000_000_000)
    raise SpanError(f"unsupported {field_name} type: {type(value).__name__}")


def _parse_duration_ns(value: Any, field_name: str = "duration_ns") -> int:
    """Parse a duration in nanoseconds; ``duration_ms`` is also accepted."""
    if isinstance(value, bool):
        raise SpanError(f"{field_name} must be numeric, got bool")
    if isinstance(value, (int, float)):
        if value < 0:
            raise SpanError(f"{field_name} must be non-negative, got {value}")
        return int(value)
    raise SpanError(f"unsupported {field_name} type: {type(value).__name__}")


@dataclass
class Span:
    trace_id: str
    span_id: str
    service: str
    name: str
    start_ns: int
    duration_ns: int
    parent_id: str | None = None
    status: str = "OK"
    attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.trace_id = _require_id(self.trace_id, "trace_id")
        self.span_id = _require_id(self.span_id, "span_id")
        if self.parent_id is not None:
            pid = self.parent_id.strip() if isinstance(self.parent_id, str) else self.parent_id
            if pid is None or pid == "":
                self.parent_id = None
            else:
                self.parent_id = _require_id(pid, "parent_id")
        if not isinstance(self.service, str) or not self.service.strip():
            raise SpanError("service must be a non-empty string")
        if not isinstance(self.name, str) or not self.name.strip():
            raise SpanError("name must be a non-empty string")
        self.service = self.service.strip()
        self.name = self.name.strip()
        self.start_ns = int(self.start_ns)
        if self.start_ns < 0:
            raise SpanError(f"start_ns must be non-negative, got {self.start_ns}")
        self.duration_ns = int(self.duration_ns)
        if self.duration_ns < 0:
            raise SpanError(
                f"duration_ns must be non-negative, got {self.duration_ns}"
            )
        if self.status not in VALID_STATUSES:
            raise SpanError(
                f"status must be one of {VALID_STATUSES}, got {self.status!r}"
            )
        if not isinstance(self.attributes, dict) or not all(
            isinstance(k, str) for k in self.attributes
        ):
            raise SpanError("attributes must be a dict with string keys")
        # values round-trip through JSON — fail fast otherwise
        try:
            import json

            json.dumps(self.attributes, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise SpanError(f"attributes must be JSON serializable: {exc}") from exc

    @property
    def end_ns(self) -> int:
        return self.start_ns + self.duration_ns

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict with a stable key order."""
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "service": self.service,
            "name": self.name,
            "start_ns": self.start_ns,
            "duration_ns": self.duration_ns,
            "status": self.status,
            "attributes": dict(sorted(self.attributes.items())),
        }

    # ---- construction helpers ------------------------------------------------

    @classmethod
    def from_dict(cls, data: Any) -> "Span":
        if not isinstance(data, dict):
            raise SpanError(f"span payload must be a JSON object, got {type(data).__name__}")

        def need(key: str) -> Any:
            if key not in data:
                raise SpanError(f"missing required field {key!r}")
            return data[key]

        attrs = data.get("attributes", {}) or {}
        if not isinstance(attrs, dict):
            raise SpanError("attributes must be an object")

        # Timestamps: canonical start_ns; accept start_ms or start (ISO text/datetime).
        if "start_ns" in data:
            start_ns = _parse_ts_ns(data["start_ns"], "start_ns")
        elif "start_ms" in data:
            start_ns = _parse_ts_ns(float(data["start_ms"]) * 1_000_000, "start_ms")
        elif "start" in data:
            start_ns = _parse_ts_ns(data["start"], "start")
        else:
            raise SpanError("missing required timestamp field 'start_ns'")

        # Durations: canonical duration_ns; accept duration_ms.
        if "duration_ns" in data:
            duration_ns = _parse_duration_ns(data["duration_ns"], "duration_ns")
        elif "duration_ms" in data:
            duration_ns = _parse_duration_ns(
                float(data["duration_ms"]) * 1_000_000, "duration_ms"
            )
        elif "duration" in data:
            duration_ns = _parse_duration_ns(data["duration"], "duration")
        else:
            raise SpanError("missing required field 'duration_ns'")

        return cls(
            trace_id=str(need("trace_id")),
            span_id=str(need("span_id")),
            parent_id=(
                str(data["parent_id"]) if data.get("parent_id") not in (None, "") else None
            ),
            service=str(need("service")),
            name=str(need("name")),
            start_ns=start_ns,
            duration_ns=duration_ns,
            status=str(data.get("status", "OK")),
            attributes=dict(attrs),
        )


def _require_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpanError(f"{field_name} must be a non-empty string")
    value = value.strip()
    if not _ID_RE.match(value):
        raise SpanError(
            f"{field_name} may only contain letters, digits and . _ - ; got {value!r}"
        )
    return value

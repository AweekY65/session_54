"""Trace model: Span, Status and timestamp helpers.

Time convention
---------------
All times stored internally as **integer microseconds since the Unix epoch**.
Inputs may be:

* int / float -> interpreted as microseconds (floats are truncated, not rounded)
* str -> ISO-8601 (e.g. ``2026-09-28T10:00:00.000001Z``; the trailing Z and
  ``+00:00`` are both accepted). Fractions beyond 6 digits are truncated.

Span identity is ``(trace_id, span_id)``: span ids only need to be unique
within a trace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

# A non-root span whose parent is missing/empty is a trace root.
ROOT_PARENT_IDS = {"", "0", "0000000000000000", "00000000000000000000000000000000"}

_ISO_FRACTION_RE = re.compile(r"^(\d{1,6})(\d*)(\d{0,6})?$")


class Status(str, Enum):
    """Span status. The string values are the on-disk representation."""

    OK = "OK"
    ERROR = "ERROR"
    UNSET = "UNSET"

    @classmethod
    def coerce(cls, value: Any) -> "Status":
        if isinstance(value, Status):
            return value
        if value is None:
            return cls.UNSET
        name = str(value).strip().upper()
        # Common OTel aliases.
        if name in ("OK", "SUCCESS", "STATUS_CODE_OK"):
            return cls.OK
        if name in ("ERROR", "FAILURE", "STATUS_CODE_ERROR", "ERR"):
            return cls.ERROR
        if name in ("UNSET", "UNKNOWN", "STATUS_CODE_UNSET", ""):
            return cls.UNSET
        raise ValueError(f"unknown status: {value!r}")


def to_micros(value: Any) -> int:
    """Convert int/float (microseconds) or ISO-8601 string to int micros."""
    if isinstance(value, bool):
        raise TypeError("timestamp must not be a bool")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("timestamp must be >= 0")
        return value
    if isinstance(value, float):
        if value < 0:
            raise ValueError("timestamp must be >= 0")
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text and (text.isdigit() or
                     (text[0] == "-" and text[1:].isdigit())):
            number = int(text)
            if number < 0:
                raise ValueError("timestamp must be >= 0")
            return number
        return _iso_to_micros(text)
    raise TypeError(f"unsupported timestamp type: {type(value).__name__}")


def _iso_to_micros(text: str) -> int:
    if not text:
        raise ValueError("empty timestamp string")
    normalized = text
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        # Retry without sub-second precision truncation quirks.
        raise ValueError(f"unparseable ISO-8601 timestamp: {text!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = dt - epoch
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def micros_to_iso(micros: int) -> str:
    """Render integer micros as a UTC ISO-8601 string with microsecond digits."""
    dt = datetime.fromtimestamp(micros / 1_000_000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _is_root_parent(parent_id: str) -> bool:
    return parent_id in ROOT_PARENT_IDS


@dataclass(frozen=True)
class Span:
    """A single span. ``duration_us`` is derived and always non-negative."""

    trace_id: str
    span_id: str
    parent_id: str  # "" marks an explicit root
    service: str
    start_us: int
    end_us: int
    status: Status = Status.UNSET
    attributes: Mapping[str, Any] = field(default_factory=dict)
    name: str = ""

    # ---- constructors -----------------------------------------------------
    def __post_init__(self) -> None:
        if not isinstance(self.trace_id, str) or not self.trace_id:
            raise ValueError("trace_id must be a non-empty string")
        if not isinstance(self.span_id, str) or not self.span_id:
            raise ValueError("span_id must be a non-empty string")
        if not isinstance(self.parent_id, str):
            raise ValueError("parent_id must be a string (use '' for a root)")
        if not isinstance(self.service, str) or not self.service:
            raise ValueError("service must be a non-empty string")
        if self.end_us < self.start_us:
            raise ValueError(
                f"end_us ({self.end_us}) precedes start_us ({self.start_us})"
            )
        # dataclass(frozen=True) allows attribute mutation via object.__setattr__;
        # normalize the status/attributes to stable, immutable forms.
        object.__setattr__(self, "status", Status.coerce(self.status))
        if not isinstance(self.attributes, Mapping):
            raise ValueError("attributes must be a mapping")
        object.__setattr__(self, "attributes", dict(self.attributes))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Span":
        """Build a Span from a plain mapping (the JSON import format).

        Accepts either ``start_us``/``end_us`` or ``start``/``end`` plus an
        optional ``duration_us`` (or ``duration_ms``) used when the end is
        omitted. Key aliases follow the common OTel/JSON conventions.
        """
        if not isinstance(data, Mapping):
            raise TypeError("span must be an object/mapping")

        def pick(*names: str, default: Any = None) -> Any:
            for n in names:
                if n in data and data[n] is not None:
                    return data[n]
            return default

        trace_id = str(pick("trace_id", "traceId"))
        if not trace_id:
            raise ValueError("trace_id is required")
        span_id = str(pick("span_id", "spanId"))
        if not span_id:
            raise ValueError("span_id is required")
        parent_raw = pick("parent_id", "parentId", "parent_span_id", default="")
        parent_id = "" if parent_raw is None else str(parent_raw)
        service = str(pick("service", "service_name", "serviceName"))
        if not service:
            raise ValueError("service is required")
        name = str(pick("name", "operation", "span_name", default=""))

        start_raw = pick("start_us", "start", "start_time", "startTime",
                         "start_time_us")
        end_raw = pick("end_us", "end", "end_time", "endTime", "end_time_us")
        if start_raw is None:
            raise ValueError("start time is required")
        start_us = to_micros(start_raw)
        if end_raw is not None:
            end_us = to_micros(end_raw)
        else:
            duration_raw = pick("duration_us", "durationUs", "duration")
            if duration_raw is not None:
                end_us = start_us + int(float(duration_raw))
            else:
                duration_ms = pick("duration_ms", "durationMs")
                if duration_ms is not None:
                    end_us = start_us + int(float(duration_ms) * 1000)
                else:
                    raise ValueError("end time or duration is required")

        attrs = pick("attributes", "attrs", "tags", default={})
        if attrs is None:
            attrs = {}
        if not isinstance(attrs, Mapping):
            raise ValueError("attributes must be an object/mapping")

        return cls(
            trace_id=trace_id,
            span_id=span_id,
            parent_id=parent_id,
            service=service,
            start_us=start_us,
            end_us=end_us,
            status=Status.coerce(pick("status", default=Status.UNSET)),
            attributes=dict(attrs),
            name=name,
        )

    # ---- serialization ----------------------------------------------------
    def to_dict(self, iso_times: bool = False) -> dict[str, Any]:
        """Stable JSON-serializable form; keys in a fixed order."""
        if iso_times:
            time_fields = {
                "start": micros_to_iso(self.start_us),
                "end": micros_to_iso(self.end_us),
            }
        else:
            time_fields = {
                "start_us": self.start_us,
                "end_us": self.end_us,
            }
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "service": self.service,
            "name": self.name,
            **time_fields,
            "duration_us": self.duration_us,
            "status": self.status.value,
            "attributes": _stable(self.attributes),
        }

    # ---- convenience ------------------------------------------------------
    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us

    @property
    def is_root(self) -> bool:
        return _is_root_parent(self.parent_id)

    def overlaps(self, start_us: int | None, end_us: int | None) -> bool:
        """Whether the span's [start, end] intersects the query window."""
        if start_us is not None and self.end_us < start_us:
            return False
        if end_us is not None and self.start_us > end_us:
            return False
        return True


def _stable(value: Any) -> Any:
    """Return a JSON-stable, deterministically-ordered copy of attributes."""
    if isinstance(value, Mapping):
        return {k: _stable(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_stable(v) for v in value]
    return value

"""Local file storage for spans and their indexes.

Directory layout (all plain files, append-only spans)::

    <data_dir>/
        spans.jsonl        # one canonical span JSON object per line
        index.json         # derived index; rebuildable, never authoritative
        meta.json          # store metadata (format version, counters)
        lock               # advisory lock file (best-effort flock)

The index is a convenience, never the source of truth: if it is missing,
corrupt, or its checksum does not match ``spans.jsonl`` it is rebuilt
automatically, and :meth:`SpanStore.reindex` forces a rebuild at any time.

Retention deletes at **trace granularity**: when any span of a trace falls
outside the retained window the whole trace is removed, so surviving
queries never return fragments.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .analysis import TraceAnalysis, analyze_tree
from .model import Span, Status
from .tree import build_trace_tree

SPANS_FILE = "spans.jsonl"
INDEX_FILE = "index.json"
META_FILE = "meta.json"
FORMAT_VERSION = 1


@dataclass
class IngestResult:
    accepted: int = 0
    duplicate_identical: int = 0
    duplicate_conflict: int = 0
    total_spans: int = 0
    trace_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "duplicate_identical": self.duplicate_identical,
            "duplicate_conflict": self.duplicate_conflict,
            "total_spans": self.total_spans,
            "trace_ids": sorted(set(self.trace_ids)),
        }


def _canonical_payload(span: Span) -> str:
    """Deterministic JSON text used both for storage and identity checks."""
    return json.dumps(span.to_dict(), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def atomic_write(path: str, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + rename, fsync)."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class SpanStore:
    """In-memory indexes backed by append-only local files."""

    def __init__(self, data_dir: str):
        self.data_dir = os.path.abspath(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.spans_path = os.path.join(self.data_dir, SPANS_FILE)
        self.index_path = os.path.join(self.data_dir, INDEX_FILE)
        self.meta_path = os.path.join(self.data_dir, META_FILE)

        # (trace_id, span_id) -> canonical payload of the winning span.
        self._payloads: dict[tuple[str, str], str] = {}
        # trace_id -> ordered list of span ids (first-seen insertion order).
        self._trace_spans: dict[str, list[str]] = {}
        # trace-level time bounds, microseconds.
        self._trace_bounds: dict[str, list[int]] = {}
        self._dirty = False

        self._load()

    # ------------------------------------------------------------------ load
    def _load(self) -> None:
        rebuilt = False
        if os.path.exists(self.index_path):
            try:
                with open(self.index_path, "r", encoding="utf-8") as fh:
                    index = json.load(fh)
                if index.get("checksum") == self._checksum():
                    self._load_from_index(index)
                else:
                    rebuilt = True
            except (ValueError, OSError, KeyError, TypeError):
                rebuilt = True
        else:
            rebuilt = True
        if rebuilt:
            self.reindex()

    def _checksum(self) -> str:
        """SHA-256 of the spans file bytes (empty string hash if absent)."""
        h = hashlib.sha256()
        if os.path.exists(self.spans_path):
            with open(self.spans_path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
        return h.hexdigest()

    def _index_span(self, span: Span, payload: str) -> None:
        key = (span.trace_id, span.span_id)
        self._payloads[key] = payload
        self._trace_spans.setdefault(span.trace_id, []).append(span.span_id)
        bounds = self._trace_bounds.setdefault(
            span.trace_id, [span.start_us, span.end_us]
        )
        if span.start_us < bounds[0]:
            bounds[0] = span.start_us
        if span.end_us > bounds[1]:
            bounds[1] = span.end_us

    def _load_from_index(self, index: Mapping[str, Any]) -> None:
        spans = self._read_spans_file()
        for span in spans:
            self._index_span(span, _canonical_payload(span))
        # If the index somehow disagrees with the file, force a rebuild.
        if len(self._payloads) != index.get("span_count"):
            self._reset_memory()
            self.reindex()

    def _reset_memory(self) -> None:
        self._payloads.clear()
        self._trace_spans.clear()
        self._trace_bounds.clear()

    def _read_spans_file(self) -> list[Span]:
        spans: list[Span] = []
        if not os.path.exists(self.spans_path):
            return spans
        with open(self.spans_path, "r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    spans.append(Span.from_dict(json.loads(line)))
                except (ValueError, TypeError) as exc:
                    raise ValueError(
                        f"corrupt span at {self.spans_path}:{line_no}: {exc}"
                    ) from exc
        return spans

    # ---------------------------------------------------------------- ingest
    def add_span(self, span: Span) -> str:
        """Ingest one span.

        Returns ``accepted`` | ``duplicate_identical`` | ``duplicate_conflict``.
        Identical redelivery (same trace/span id and same payload) is a
        no-op. A reused id with different payload is rejected (first wins).
        """
        payload = _canonical_payload(span)
        key = (span.trace_id, span.span_id)
        existing = self._payloads.get(key)
        if existing is not None:
            if existing == payload:
                return "duplicate_identical"
            return "duplicate_conflict"
        with open(self.spans_path, "a", encoding="utf-8") as fh:
            fh.write(payload + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._index_span(span, payload)
        self._dirty = True
        return "accepted"

    def add_spans(self, spans: Iterable[Span | Mapping[str, Any]]) -> IngestResult:
        result = IngestResult()
        for item in spans:
            span = item if isinstance(item, Span) else Span.from_dict(item)
            outcome = self.add_span(span)
            if outcome == "accepted":
                result.accepted += 1
            elif outcome == "duplicate_identical":
                result.duplicate_identical += 1
            else:
                result.duplicate_conflict += 1
            result.trace_ids.append(span.trace_id)
        result.total_spans = len(self._payloads)
        return result

    def import_file(self, path: str) -> IngestResult:
        """Import spans from a local JSON file (a list) or JSONL file."""
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        spans: list[Mapping[str, Any]]
        stripped = text.strip()
        if not stripped:
            return IngestResult(total_spans=len(self._payloads))
        lines = [ln for ln in stripped.splitlines() if ln.strip()]
        try:
            data = json.loads(stripped)
        except ValueError:
            # Not one JSON document -> must be JSONL, one span per line.
            spans = [json.loads(line) for line in lines]
        else:
            if isinstance(data, list):
                spans = data
            elif isinstance(data, dict) and isinstance(data.get("spans"), list):
                spans = data["spans"]
            else:
                spans = [data]
        return self.add_spans(spans)

    # ---------------------------------------------------------------- access
    def __len__(self) -> int:
        return len(self._payloads)

    def trace_ids(self) -> list[str]:
        return sorted(self._trace_spans)

    def get_span(self, trace_id: str, span_id: str) -> Span | None:
        payload = self._payloads.get((trace_id, span_id))
        return Span.from_dict(json.loads(payload)) if payload is not None else None

    def get_trace_spans(self, trace_id: str) -> list[Span]:
        ids = self._trace_spans.get(trace_id, ())
        return [self.get_span(trace_id, sid) for sid in ids]

    def iter_traces(self) -> Iterable[tuple[str, list[Span]]]:
        for trace_id in self.trace_ids():
            yield trace_id, self.get_trace_spans(trace_id)

    def trace_bounds(self, trace_id: str) -> tuple[int, int] | None:
        bounds = self._trace_bounds.get(trace_id)
        return (bounds[0], bounds[1]) if bounds else None

    # ------------------------------------------------------------- analytics
    def analyze_trace(self, trace_id: str) -> TraceAnalysis | None:
        spans = self.get_trace_spans(trace_id)
        if not spans:
            return None
        return analyze_tree(build_trace_tree(spans))

    def analyses(self) -> list[TraceAnalysis]:
        return [a for a in (self.analyze_trace(t) for t in self.trace_ids())
                if a is not None]

    # ----------------------------------------------------------- maintenance
    def reindex(self) -> dict[str, Any]:
        """Rebuild every in-memory index and ``index.json`` from spans.jsonl."""
        self._reset_memory()
        spans = self._read_spans_file()
        for span in spans:
            self._index_span(span, _canonical_payload(span))
        index = {
            "format_version": FORMAT_VERSION,
            "span_count": len(self._payloads),
            "trace_count": len(self._trace_spans),
            "checksum": self._checksum(),
        }
        atomic_write(
            self.index_path,
            json.dumps(index, sort_keys=True, indent=2) + "\n",
        )
        self._write_meta()
        self._dirty = False
        return dict(index)

    def _write_meta(self) -> None:
        meta = {
            "format_version": FORMAT_VERSION,
            "span_count": len(self._payloads),
            "trace_count": len(self._trace_spans),
        }
        atomic_write(
            self.meta_path,
            json.dumps(meta, sort_keys=True, indent=2) + "\n",
        )

    def flush(self) -> None:
        """Persist derived indexes (spans themselves are already durable)."""
        self.reindex()

    def retain(self, before_us: int | None = None, *,
               after_us: int | None = None,
               max_traces: int | None = None) -> dict[str, Any]:
        """Delete traces outside the retained window / over the trace cap.

        ``before_us``
            Delete traces that *ended at or before* this cutoff — the
            standard "drop data older than T" rule.

        ``after_us``
            Delete traces that *started at or after* this cutoff (drop data
            newer than T, mostly useful in tests).

        ``max_traces``
            Keep the N most recently started traces (ties broken by id).

        Deletion is whole-trace and rewrites ``spans.jsonl`` atomically,
        then rebuilds the index, so later queries cannot return removed data.
        """
        before = {"trace_count": len(self._trace_spans),
                  "span_count": len(self._payloads)}

        doomed: set[str] = set()
        for trace_id, bounds in self._trace_bounds.items():
            t_start, t_end = bounds
            if before_us is not None and t_end <= before_us:
                doomed.add(trace_id)
            if after_us is not None and t_start >= after_us:
                doomed.add(trace_id)

        if max_traces is not None and max_traces >= 0:
            ordered = sorted(
                self._trace_bounds,
                key=lambda t: (-self._trace_bounds[t][0], t),
            )
            doomed.update(ordered[max_traces:])

        if doomed:
            surviving = [
                span for trace_id, ids in self._trace_spans.items()
                if trace_id not in doomed
                for sid in ids
                for span in [self.get_span(trace_id, sid)]
                if span is not None
            ]
            lines = [_canonical_payload(s) + "\n" for s in surviving]
            atomic_write(self.spans_path, "".join(lines))
            self.reindex()
        else:
            # Still make sure the index is fresh.
            self.flush()

        after = {"trace_count": len(self._trace_spans),
                 "span_count": len(self._payloads)}
        return {
            "deleted_trace_count": before["trace_count"] - after["trace_count"],
            "deleted_span_count": before["span_count"] - after["span_count"],
            "deleted_trace_ids": sorted(doomed),
            "remaining_trace_count": after["trace_count"],
            "remaining_span_count": after["span_count"],
        }

    # ------------------------------------------------------------- utilities
    def status_counts(self) -> dict[str, int]:
        """Trace-level status histogram (ERROR if any span errored)."""
        counts = {Status.OK.value: 0, Status.ERROR.value: 0,
                  Status.UNSET.value: 0}
        for analysis in self.analyses():
            counts[analysis.status.value] += 1
        return counts

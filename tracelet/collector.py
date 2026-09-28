"""The collector: accept spans (any order), persist, analyze, query, retain.

Typical lifecycle::

    c = Collector(LocalStore("./trace-data"))
    c.ingest(span)                 # accepts children before parents
    c.reindex()                    # rebuild trees, indexes and statistics
    c.get(trace_id)                # one analyzed trace (dict)
    c.query(filters=...)           # filtered traces
    c.retention(keep_last=100)     # delete old traces everywhere

Ingestion never blocks on a missing parent: spans are simply appended to the
local span log; tree reconstruction happens in :meth:`reindex`, which sees
both sides of every parent/child link regardless of arrival order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from . import analyzer as az
from .analyzer import Trace
from .model import Span, ModelError
from .query import Filter, list_traces
from .storage import LocalStore


@dataclass
class IngestResult:
    accepted: int = 0
    rejected: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "errors": list(self.errors),
        }


class Collector:
    def __init__(self, store: Optional[LocalStore] = None,
                 data_dir: Optional[str] = None):
        if store is None:
            store = LocalStore(data_dir)
        self.store = store
        self._traces: dict[str, dict] = {}

    # --------------------------------------------------------------- ingest
    def ingest(self, span: Span) -> IngestResult:
        return self.ingest_many([span])

    def ingest_dict(self, raw: dict) -> IngestResult:
        return self.ingest_many_dicts([raw])

    def ingest_many(self, spans: Iterable[Span]) -> IngestResult:
        result = IngestResult()
        existing = {(s.trace_id, s.span_id) for s in self.store.load_spans()}
        pending: list[Span] = []
        for sp in spans:
            key = (sp.trace_id, sp.span_id)
            if key in existing:
                result.duplicates += 1
                result.errors.append(
                    f"duplicate span id {sp.span_id!r} in trace "
                    f"{sp.trace_id!r}: already stored, ignored")
                continue
            existing.add(key)
            pending.append(sp)
            result.accepted += 1
        self.store.append_spans(pending)
        # Stored data changed: cached analysis is stale until next reindex.
        self._traces.clear()
        return result

    def ingest_many_dicts(self, raws: Iterable[dict]) -> IngestResult:
        result = IngestResult()
        good: list[Span] = []
        for i, raw in enumerate(raws):
            try:
                good.append(Span.from_dict(raw))
            except ModelError as exc:
                result.rejected += 1
                result.errors.append(f"record {i}: {exc}")
        if good:
            sub = self.ingest_many(good)
            result.accepted += sub.accepted
            result.duplicates += sub.duplicates
            result.errors.extend(sub.errors)
        self._traces.clear()
        return result

    def import_jsonl(self, path: str | Path) -> IngestResult:
        """Import spans from a local .jsonl/.ndjson file (one JSON/line)."""
        raws: list[dict] = []
        with Path(path).open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    raws.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    r = IngestResult(rejected=1,
                                     errors=[f"line {line_no}: {exc}"])
                    return r
        return self.ingest_many_dicts(raws)

    def import_json_array(self, path: str | Path) -> IngestResult:
        """Import spans from a local JSON file containing a span array."""
        with Path(path).open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise ValueError("JSON import expects an array of span objects")
        return self.ingest_many_dicts(data)

    # -------------------------------------------------------------- analyze
    def analyze_trace(self, trace_id: str) -> Optional[Trace]:
        spans = [s for s in self.store.load_spans() if s.trace_id == trace_id]
        if not spans:
            return None
        return az.build_trace(spans)

    def reindex(self) -> dict[str, Trace]:
        """Rebuild every trace tree, index file and the global summary.

        This is the index-rebuild operation: it starts from the raw span log
        and recomputes everything, so it also repairs/repopulates a missing or
        stale ``index/`` directory.
        """
        traces = az.build_traces(self.store.load_spans())
        self.store.clear_indexes()
        events: list[dict] = []
        for td in traces.values():
            payload = td.to_dict()
            self.store.write_index(payload)
            for anom in td.anomalies:
                ev = anom.to_dict()
                ev["trace_id"] = td.trace_id
                events.append(ev)
        self.store.write_events(events)
        self.store.write_summary(self._summarize(traces.values(), events))
        self._traces = {tid: td.to_dict() for tid, td in traces.items()}
        return traces

    @staticmethod
    def _summarize(traces: Iterable[Trace], events: list[dict]) -> dict:
        traces = list(traces)
        n = len(traces)
        errors = sum(1 for t in traces if t.is_error())
        total_spans = sum(t.span_count for t in traces)
        durations = sorted(t.duration_us for t in traces)
        services: dict[str, int] = {}
        anomaly_counts: dict[str, int] = {}
        for t in traces:
            for svc in t.service_time_us:
                services[svc] = services.get(svc, 0) + 1
            for a in t.anomalies:
                anomaly_counts[a.kind] = anomaly_counts.get(a.kind, 0) + 1

        def pct(p: float) -> Optional[int]:
            if not durations:
                return None
            k = min(len(durations) - 1,
                    max(0, int(round((p / 100.0) * (len(durations) - 1)))))
            return durations[k]

        return {
            "trace_count": n,
            "span_count": total_spans,
            "error_trace_count": errors,
            "ok_trace_count": n - errors,
            "services": dict(sorted(services.items())),
            "anomaly_counts": dict(sorted(anomaly_counts.items())),
            "duration_us": {
                "min": durations[0] if durations else None,
                "max": durations[-1] if durations else None,
                "avg": (sum(durations) // n) if n else None,
                "p50": pct(50),
                "p95": pct(95),
                "p99": pct(99),
            },
        }

    # ---------------------------------------------------------------- query
    def _ensure_analyzed(self) -> dict[str, dict]:
        if not self._traces:
            traces = az.build_traces(self.store.load_spans())
            self._traces = {tid: t.to_dict() for tid, t in traces.items()}
        return self._traces

    def all_traces(self) -> list[dict]:
        return list_traces(self._ensure_analyzed().values())

    def get(self, trace_id: str) -> Optional[dict]:
        # Prefer a persisted index, fall back to in-memory analysis.
        cached = self._ensure_analyzed().get(trace_id)
        if cached is not None:
            return cached
        return self.store.read_index(trace_id)

    def query(self, filters: Optional[Filter] = None,
              limit: Optional[int] = None) -> list[dict]:
        return list_traces(self._ensure_analyzed().values(), filters, limit)

    def summary(self) -> dict:
        s = self.store.read_summary()
        if s is not None:
            return s
        return self._summarize(az.build_traces(self.store.load_spans()).values(),
                               [])

    # ------------------------------------------------------------ retention
    def retention(self, keep_last: Optional[int] = None,
                  max_age_us: Optional[int] = None,
                  now_us: Optional[int] = None,
                  reindex: bool = True) -> dict:
        """Delete traces (spans + index + events) locally.

        Selection rules:
          * ``max_age_us``: drop traces whose end time is older than
            ``now_us - max_age_us`` (``now_us`` defaults to the latest end
            seen, so the operation is deterministic in tests);
          * ``keep_last`` : after age filtering, keep the N most recent
            traces and drop the rest.

        After rewriting the span/event logs and removing index files a
        reindex runs (unless disabled), so subsequent queries provably cannot
        return deleted data.
        """
        spans = self.store.load_spans()
        traces = az.build_traces(spans)
        ordered = sorted(traces.values(),
                         key=lambda t: (t.end_us, t.trace_id))
        if now_us is None:
            now_us = max((t.end_us for t in ordered), default=0)

        keep: set[str] = set()
        for t in ordered:
            if max_age_us is not None and t.end_us < now_us - max_age_us:
                continue
            keep.add(t.trace_id)

        if keep_last is not None:
            recent = [t for t in ordered if t.trace_id in keep]
            keep = {t.trace_id for t in recent[-keep_last:]} \
                if keep_last >= 0 else set()

        kept_spans = [s for s in spans if s.trace_id in keep]
        kept_events = [e for e in self.store.load_events()
                       if e.get("trace_id") in keep]

        for tid in traces:
            if tid not in keep:
                self.store.delete_index(tid)
        self.store.rewrite_logs(kept_spans, kept_events)
        self._traces.clear()

        deleted = len(traces) - len(keep)
        if reindex:
            self.reindex()
        return {
            "deleted_trace_count": deleted,
            "kept_trace_count": len(keep),
            "deleted_span_count": len(spans) - len(kept_spans),
        }

"""Collector: the write/read façade over :class:`~localtrace.store.SpanStore`.

Also exposes a tiny in-process pub/sub so tests can subscribe to accepted
spans without touching any network.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

from .model import Span
from .query import TraceFilter, query_traces
from .analysis import TraceAnalysis
from .store import IngestResult, SpanStore

SpanListener = Callable[[Span], None]


class Collector:
    """Local collector — holds a SpanStore and fans accepted spans out."""

    def __init__(self, store: SpanStore | str):
        self.store = store if isinstance(store, SpanStore) else SpanStore(store)
        self._listeners: list[SpanListener] = []

    def subscribe(self, listener: SpanListener) -> None:
        self._listeners.append(listener)

    def receive(self, spans: Iterable[Span | Mapping[str, Any]]) -> IngestResult:
        """Accept an iterable of Spans (or dicts), in any arrival order."""
        result = IngestResult()
        for item in spans:
            span = item if isinstance(item, Span) else Span.from_dict(item)
            outcome = self.store.add_span(span)
            if outcome == "accepted":
                result.accepted += 1
                for listener in list(self._listeners):
                    listener(span)
            elif outcome == "duplicate_identical":
                result.duplicate_identical += 1
            else:
                result.duplicate_conflict += 1
            result.trace_ids.append(span.trace_id)
        result.total_spans = len(self.store)
        return result

    def receive_one(self, span: Span | Mapping[str, Any]) -> str:
        """Accept one span and return its outcome string."""
        span = span if isinstance(span, Span) else Span.from_dict(span)
        outcome = self.store.add_span(span)
        if outcome == "accepted":
            for listener in list(self._listeners):
                listener(span)
        return outcome
    def query(self, flt: TraceFilter | None = None, *,
              limit: int | None = None) -> list[TraceAnalysis]:
        return query_traces(self.store, flt, limit=limit)

    def flush(self) -> None:
        self.store.flush()

    def close(self) -> None:
        self.flush()
        self._listeners.clear()

    def __enter__(self) -> "Collector":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

"""Storage: persistence, reload, dedup, index rebuild, retention."""

import json
import os

import pytest

from conftest import make_span
from localtrace.model import Span
from localtrace.store import SpanStore, INDEX_FILE, SPANS_FILE


@pytest.fixture
def store(tmp_path):
    return SpanStore(str(tmp_path / "data"))


def test_ingest_and_persist(store):
    result = store.add_spans([
        make_span(trace_id="t1", span_id="a", service="svc",
                  start=0, end=10),
        make_span(trace_id="t1", span_id="b", parent_id="a", service="svc2",
                  start=2, end=8),
    ])
    assert result.accepted == 2
    assert os.path.exists(store.spans_path)
    # one canonical JSON object per line
    with open(store.spans_path, encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln]
    assert len(lines) == 2
    for ln in lines:
        json.loads(ln)  # each line parses independently


def test_reload_reconstructs(store, tmp_path):
    store.add_spans([
        make_span(trace_id="t1", span_id="a", start=0, end=10),
        make_span(trace_id="t1", span_id="b", parent_id="a", start=2, end=8),
    ])
    store.flush()
    reloaded = SpanStore(store.data_dir)
    assert len(reloaded) == 2
    assert reloaded.trace_bounds("t1") == (0, 10)
    ids = [s.span_id for s in reloaded.get_trace_spans("t1")]
    assert ids == ["a", "b"]


def test_identical_redelivery_is_noop(store):
    span = make_span(span_id="a", start=0, end=10, env="prod")
    assert store.add_span(span) == "accepted"
    assert store.add_span(span) == "duplicate_identical"
    assert len(store) == 1


def test_conflicting_duplicate_rejected(store):
    store.add_span(make_span(span_id="a", service="svc-a", start=0, end=10))
    outcome = store.add_span(
        make_span(span_id="a", service="svc-b", start=0, end=10))
    assert outcome == "duplicate_conflict"
    assert store.get_span("t", "a").service == "svc-a"
    assert len(store) == 1


def test_reindex_from_spans_file(store):
    store.add_spans([
        make_span(trace_id="t1", span_id="a", start=0, end=10),
        make_span(trace_id="t2", span_id="a", start=20, end=30),
    ])
    # Corrupt the index: a fresh store must self-heal from spans.jsonl.
    with open(store.index_path, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    healed = SpanStore(store.data_dir)
    assert len(healed) == 2
    assert set(healed.trace_ids()) == {"t1", "t2"}

    # Explicit rebuild returns fresh counters and a matching checksum.
    info = healed.reindex()
    assert info["span_count"] == 2 and info["trace_count"] == 2
    with open(healed.index_path, encoding="utf-8") as fh:
        index = json.load(fh)
    assert index["checksum"] == healed._checksum()


def test_import_json_array_and_jsonl(store, tmp_path):
    p_json = tmp_path / "spans.json"
    p_json.write_text(json.dumps([
        make_span(trace_id="t1", span_id="a").to_dict(),
        make_span(trace_id="t1", span_id="b", parent_id="a").to_dict(),
    ]), encoding="utf-8")
    assert store.import_file(str(p_json)).accepted == 2

    p_jsonl = tmp_path / "more.jsonl"
    p_jsonl.write_text("\n".join([
        json.dumps(make_span(trace_id="t2", span_id="a").to_dict()),
        json.dumps(make_span(trace_id="t2", span_id="b", parent_id="a").to_dict()),
    ]), encoding="utf-8")
    assert store.import_file(str(p_jsonl)).accepted == 2
    assert len(store) == 4


def test_retention_time_window_deletes_whole_traces(store):
    store.add_spans([
        # old trace: 0..100
        make_span(trace_id="old", span_id="a", start=0, end=100),
        make_span(trace_id="old", span_id="b", parent_id="a", start=10, end=90),
        # new trace: 200..300
        make_span(trace_id="new", span_id="a", start=200, end=300),
    ])
    summary = store.retain(before_us=150)
    assert summary["deleted_trace_count"] == 1
    assert summary["deleted_trace_ids"] == ["old"]
    assert store.trace_ids() == ["new"]
    # deleted spans are gone from the backing file too
    with open(store.spans_path, encoding="utf-8") as fh:
        content = fh.read()
    assert "old" not in content and "new" in content


def test_queries_after_retention_return_nothing_deleted(store):
    from localtrace.query import TraceFilter, query_traces

    store.add_spans([
        make_span(trace_id="old", span_id="a", service="svc",
                  start=0, end=100),
        make_span(trace_id="new", span_id="a", service="svc",
                  start=200, end=300),
    ])
    store.retain(after_us=150)  # drop traces starting at/after 150
    hits = query_traces(store, TraceFilter(services=["svc"]))
    assert [h.trace_id for h in hits] == ["old"]
    assert store.get_span("new", "a") is None

    # and the conventional "drop older than T" via before_us
    store.add_span(make_span(trace_id="new", span_id="a", service="svc",
                             start=200, end=300))
    store.retain(before_us=150)
    assert store.trace_ids() == ["new"]


def test_retention_max_traces_keeps_recent(store):
    for i, tid in enumerate(["t0", "t1", "t2", "t3"]):
        store.add_span(make_span(trace_id=tid, span_id="a",
                                 start=i * 100, end=i * 100 + 50))
    summary = store.retain(max_traces=2)
    assert summary["deleted_trace_count"] == 2
    assert store.trace_ids() == ["t2", "t3"]


def test_retention_survives_reload(store):
    store.add_spans([
        make_span(trace_id="old", span_id="a", start=0, end=10),
        make_span(trace_id="new", span_id="a", start=200, end=210),
    ])
    store.retain(before_us=100)
    reloaded = SpanStore(store.data_dir)
    assert reloaded.trace_ids() == ["new"]

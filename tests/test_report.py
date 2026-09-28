"""Reports: stable JSON/text output and end-to-end ingestion flow."""

import json

from conftest import make_span
from localtrace.collector import Collector
from localtrace.model import Status
from localtrace.query import TraceFilter, query_traces
from localtrace.report import build_report, report_json, report_text
from localtrace.store import SpanStore


def _populate(store):
    collector = Collector(store)
    # Deliberately shuffled arrival order (children before parents).
    collector.receive([
        make_span(trace_id="t1", span_id="gc", parent_id="c1", service="db",
                  start=20, end=60),
        make_span(trace_id="t1", span_id="c1", parent_id="r", service="api",
                  start=10, end=80),
        make_span(trace_id="t1", span_id="r", service="gateway",
                  start=0, end=100),
        make_span(trace_id="t2", span_id="r", service="gateway",
                  start=200, end=260, status=Status.ERROR, env="prod"),
    ])
    return collector


def test_json_report_stable(tmp_path):
    s1 = SpanStore(str(tmp_path / "r1"))
    _populate(s1)
    analyses = query_traces(s1, TraceFilter())
    report = build_report(s1, analyses, detailed=True)

    text_a = report_json(report)
    text_b = report_json(build_report(s1, analyses, detailed=True))
    assert text_a == text_b
    parsed = json.loads(text_a)
    assert parsed["store"] == {"total_traces": 2, "total_spans": 4}
    assert parsed["query"]["matched_traces"] == 2
    # gateway: t1 root 20 (100 - c1 70.. wait union: c1 10..80 -> 30? recompute)
    # t1 root 0..100, c1 10..80, gc 20..60 (child of c1 not root)
    # root children union = 10..80 (70) -> root exclusive 30
    # t2 root 200..260 no children -> exclusive 60 ; gateway total 90
    assert parsed["statistics"]["service_exclusive_us"]["gateway"] == 90
    # detailed tree present and ordered root-first
    t1 = next(t for t in parsed["traces"] if t["trace_id"] == "t1")
    assert t1["roots"][0]["span_id"] == "r"
    assert t1["roots"][0]["children"][0]["span_id"] == "c1"


def test_text_report_contains_key_sections(tmp_path):
    store = SpanStore(str(tmp_path / "rt"))
    _populate(store)
    analyses = query_traces(store, TraceFilter())
    text = report_text(build_report(store, analyses, detailed=True))
    assert "LOCALTRACE REPORT" in text
    assert "trace t1" in text and "trace t2" in text
    assert "critical path" in text
    assert "ERROR" in text


def test_summary_report_when_not_detailed(tmp_path):
    store = SpanStore(str(tmp_path / "rs"))
    _populate(store)
    analyses = query_traces(store, TraceFilter())
    parsed = json.loads(report_json(build_report(store, analyses)))
    assert parsed["traces"] == []
    assert {s["trace_id"] for s in parsed["trace_summaries"]} == {"t1", "t2"}


def test_receive_out_of_order_links_preserved(tmp_path):
    store = SpanStore(str(tmp_path / "ooo"))
    _populate(store)  # gc arrived first, r last
    analysis = store.analyze_trace("t1")
    assert analysis.tree.roots == ["r"]
    assert analysis.tree.children["r"] == ["c1"]
    assert analysis.tree.children["c1"] == ["gc"]
    assert analysis.anomalies == []
    assert analysis.duration_us == 100


def test_subscriber_invoked_on_accept_only(tmp_path):
    store = SpanStore(str(tmp_path / "sub"))
    collector = Collector(store)
    seen = []
    collector.subscribe(seen.append)
    span = make_span(span_id="a")
    collector.receive_one(span)
    collector.receive_one(span)  # identical duplicate -> no fan-out
    assert [s.span_id for s in seen] == ["a"]

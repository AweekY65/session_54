"""End-to-end tests for the local collector: storage, query, retention."""

import json
import tempfile
import unittest
from pathlib import Path

from tracelet.analyzer import ORPHAN, CYCLE
from tracelet.collector import Collector
from tracelet.model import Span
from tracelet.query import make_filter
from tracelet.storage import LocalStore


def sp(span_id, parent, service, start, dur, *, tid="t1", name="op",
       status="OK", **attrs):
    return Span(trace_id=tid, span_id=span_id, parent_id=parent,
                service=service, name=name, start_us=start,
                end_us=start + dur, status=status, attributes=dict(attrs))


class CollectorIngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.c = Collector(LocalStore(self.tmp.name))

    def test_out_of_order_ingestion_links_late_parent(self):
        # child first, then grandchild, then the parent last
        self.c.ingest_many([sp("c", "root", "svc", 10, 20),
                            sp("gc", "c", "svc", 12, 5)])
        self.c.ingest(sp("root", None, "gw", 0, 100))
        td = self.c.reindex()["t1"].to_dict()
        self.assertEqual(td["span_count"], 3)
        self.assertEqual([r["span"]["span_id"] for r in td["roots"]],
                         ["root"])
        self.assertEqual(td["anomalies"], [])

    def test_query_before_parent_arrives_reports_orphan_then_resolves(self):
        self.c.ingest(sp("c", "root", "svc", 10, 20))
        partial = self.c.all_traces()[0]
        self.assertEqual([a["kind"] for a in partial["anomalies"]], [ORPHAN])
        self.c.ingest(sp("root", None, "gw", 0, 100))
        resolved = self.c.all_traces()[0]
        self.assertEqual(resolved["anomalies"], [])

    def test_duplicate_ingest_rejected_at_collector(self):
        r1 = self.c.ingest(sp("s1", None, "a", 0, 10))
        self.assertEqual(r1.accepted, 1)
        r2 = self.c.ingest(sp("s1", None, "b", 0, 10))
        self.assertEqual(r2.duplicates, 1)
        self.assertEqual(r2.accepted, 0)
        traces = self.c.reindex()
        self.assertEqual(traces["t1"].span_count, 1)
        self.assertEqual(traces["t1"].nodes["s1"].span.service, "a")

    def test_duplicate_within_one_batch(self):
        r = self.c.ingest_many([
            sp("s", None, "a", 0, 10),
            sp("s", None, "b", 0, 10),
        ])
        self.assertEqual((r.accepted, r.duplicates), (1, 1))

    def test_orphan_and_error_records(self):
        self.c.ingest_many([
            sp("root", None, "gw", 0, 100),
            sp("bad", "missing", "svc", 0, 5, status="ERROR"),
        ])
        self.c.reindex()
        td = self.c.get("t1")
        self.assertEqual(td["status"], "ERROR")
        self.assertEqual([a["kind"] for a in td["anomalies"]], [ORPHAN])

    def test_invalid_records_rejected(self):
        r = self.c.ingest_many_dicts([
            {"span_id": "ok", "trace_id": "t1", "service": "s",
             "start_us": 0, "end_us": 1},
            {"span_id": "bad", "trace_id": "t1", "service": "s",
             "start_us": 10, "end_us": 1},  # end < start
            {"trace_id": "t1"},  # missing span id / service
        ])
        self.assertEqual(r.accepted, 1)
        self.assertEqual(r.rejected, 2)

    def test_cycle_persisted_and_rebuilt(self):
        self.c.ingest_many([
            Span("t1", "a", "b", "s", "op", 0, 10),
            Span("t1", "b", "a", "s", "op", 0, 10),
        ])
        self.c.reindex()
        # events file carries the cycle
        events = self.c.store.load_events()
        self.assertTrue(any(e["kind"] == CYCLE for e in events))
        td = self.c.get("t1")
        self.assertTrue(any(a["kind"] == CYCLE for a in td["anomalies"]))


class PersistenceAndReindexTests(unittest.TestCase):
    def test_files_written_and_reopened(self):
        with tempfile.TemporaryDirectory() as d:
            c = Collector(LocalStore(d))
            c.ingest_many([sp("root", None, "gw", 0, 100),
                           sp("c", "root", "svc", 10, 20)])
            c.reindex()
            self.assertTrue((Path(d) / "spans.jsonl").exists())
            self.assertTrue((Path(d) / "index" / "tr_t1.json").exists())
            self.assertTrue((Path(d) / "index" / "summary.json").exists())

            # Reopen: rebuild purely from the span log (indexes deleted).
            c2 = Collector(LocalStore(d))
            c2.store.clear_indexes()
            traces = c2.reindex()
            td = traces["t1"]
            self.assertEqual(td.span_count, 2)
            self.assertEqual(td.service_time_us["svc"], 20)

    def test_in_memory_store_touches_no_files(self):
        c = Collector(LocalStore(None))
        c.ingest_many([sp("root", None, "gw", 0, 10)])
        self.assertIsNone(c.store.path)
        td = c.reindex()["t1"].to_dict()
        self.assertEqual(td["span_count"], 1)

    def test_jsonl_import_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            payload = [
                sp("root", None, "gw", 0, 100, tid="T").to_dict(),
                sp("c", "root", "svc", 10, 20, tid="T").to_dict(),
            ]
            p = Path(d) / "spans.jsonl"
            p.write_text("\n".join(json.dumps(x) for x in payload),
                         encoding="utf-8")
            c = Collector(LocalStore(Path(d) / "data"))
            r = c.import_jsonl(p)
            self.assertEqual(r.accepted, 2)
            self.assertEqual(c.reindex()["T"].span_count, 2)


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.c = Collector(LocalStore(None))
        self.c.ingest_many([
            # trace A: gateway + payment error, attr amount=4200
            sp("a-root", None, "gateway", 0, 100, tid="A",
               http_route="/pay"),
            sp("a-pay", "a-root", "payment", 10, 50, tid="A",
               status="ERROR", amount=4200),
            # trace B: gateway + inventory OK, later time window
            sp("b-root", None, "gateway", 1000, 100, tid="B",
               http_route="/stock"),
            sp("b-inv", "b-root", "inventory", 1010, 40, tid="B"),
        ])
        self.c.reindex()

    def test_filter_service(self):
        got = self.c.query(make_filter(service="payment"))
        self.assertEqual([t["trace_id"] for t in got], ["A"])

    def test_filter_status_error(self):
        got = self.c.query(make_filter(status="ERROR"))
        self.assertEqual([t["trace_id"] for t in got], ["A"])
        self.assertEqual(self.c.query(make_filter(status="OK"))[0]
                         ["trace_id"], "B")

    def test_filter_attribute_presence_and_equality(self):
        self.assertEqual(
            [t["trace_id"] for t in self.c.query(make_filter(attr="amount"))],
            ["A"])
        self.assertEqual(
            [t["trace_id"]
             for t in self.c.query(make_filter(attr="amount=4200"))],
            ["A"])
        self.assertEqual(
            self.c.query(make_filter(attr="amount=9999")), [])
        self.assertEqual(
            [t["trace_id"]
             for t in self.c.query(make_filter(attr="http_route=/stock"))],
            ["B"])

    def test_filter_time_range_overlap(self):
        # window fully inside A
        self.assertEqual(
            [t["trace_id"]
             for t in self.c.query(make_filter(start=0, end=50))], ["A"])
        # window covering both
        self.assertEqual(
            [t["trace_id"]
             for t in self.c.query(make_filter(start=0, end=2000))],
            ["A", "B"])
        # window after A
        self.assertEqual(
            [t["trace_id"]
             for t in self.c.query(make_filter(start=500, end=None))],
            ["B"])

    def test_combined_filters_and_ordering(self):
        got = self.c.query(make_filter(service="gateway", status="ERROR"))
        self.assertEqual([t["trace_id"] for t in got], ["A"])
        ordered = self.c.query()
        self.assertEqual([t["trace_id"] for t in ordered], ["A", "B"])


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.c = Collector(LocalStore(self.tmp.name))
        self.c.ingest_many([
            sp("r1", None, "gw", 0, 10, tid="old"),
            sp("r2", None, "gw", 1_000, 10, tid="mid"),
            sp("r3", None, "gw", 2_000, 10, tid="new"),
        ])
        self.c.reindex()

    def test_keep_last(self):
        info = self.c.retention(keep_last=1)
        self.assertEqual(info["deleted_trace_count"], 2)
        ids = [t["trace_id"] for t in self.c.query()]
        self.assertEqual(ids, ["new"])

    def test_max_age(self):
        # now = latest end = 2010; keep traces ending >= now-500 -> [new]
        info = self.c.retention(max_age_us=500)
        self.assertEqual(info["deleted_trace_count"], 2)
        ids = [t["trace_id"] for t in self.c.query()]
        self.assertEqual(ids, ["new"])

    def test_deleted_data_gone_everywhere(self):
        self.c.retention(keep_last=1)
        # span log
        stored = {s.trace_id for s in self.c.store.load_spans()}
        self.assertEqual(stored, {"new"})
        # index files
        self.assertEqual(self.c.store.list_index_ids(), ["new"])
        self.assertIsNone(self.c.get("old"))
        # raw file content really lacks the deleted trace
        raw = (Path(self.tmp.name) / "spans.jsonl").read_text()
        self.assertNotIn("old", raw)
        self.assertNotIn("mid", raw)
        self.assertIn("new", raw)

    def test_retention_then_reindex_stable(self):
        self.c.retention(keep_last=2)
        self.c.reindex()
        ids = sorted(t["trace_id"] for t in self.c.query())
        self.assertEqual(ids, ["mid", "new"])
        summary = self.c.summary()
        self.assertEqual(summary["trace_count"], 2)


class SummaryTests(unittest.TestCase):
    def test_summary_counts(self):
        c = Collector(LocalStore(None))
        c.ingest_many([
            sp("r1", None, "gw", 0, 10, tid="A"),
            sp("r2", None, "gw", 100, 30, tid="B", status="ERROR"),
            sp("x", "ghost", "svc", 0, 5, tid="B"),
        ])
        c.reindex()
        s = c.summary()
        self.assertEqual(s["trace_count"], 2)
        self.assertEqual(s["span_count"], 3)
        self.assertEqual(s["error_trace_count"], 1)
        self.assertIn("gw", s["services"])
        self.assertEqual(s["anomaly_counts"].get(ORPHAN), 1)
        self.assertEqual(s["duration_us"]["min"], 10)
        self.assertEqual(s["duration_us"]["max"], 130)


if __name__ == "__main__":
    unittest.main()

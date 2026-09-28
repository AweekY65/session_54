"""Stability tests for JSON/text reports."""

import json
import unittest

from tracelet import report
from tracelet.analyzer import build_trace
from tracelet.model import Span


def _trace():
    spans = [
        Span("t1", "root", None, "gw", "GET /", 0, 100, attributes={"z": 1}),
        Span("t1", "c1", "root", "svc-a", "a", 10, 40),
        Span("t1", "c2", "root", "svc-b", "b", 60, 80, status="ERROR"),
    ]
    return build_trace(spans).to_dict()


class ReportTests(unittest.TestCase):
    def test_json_is_stable(self):
        td = _trace()
        first = report.to_json(td)
        second = report.to_json(td)
        self.assertEqual(first, second)  # deterministic
        parsed = json.loads(first)
        self.assertEqual(parsed["trace_id"], "t1")
        self.assertEqual(parsed["status"], "ERROR")
        # sort_keys=True -> keys are alphabetical
        self.assertEqual(list(parsed.keys()), sorted(parsed.keys()))

    def test_text_contains_sections(self):
        text = report.to_text(_trace())
        self.assertIn("Trace t1", text)
        self.assertIn("critical path", text)
        self.assertIn("service time", text)
        self.assertIn("svc-b", text)

    def test_summary_text(self):
        out = report.summary_to_text({
            "trace_count": 1, "ok_trace_count": 1, "error_trace_count": 0,
            "span_count": 2,
            "duration_us": {"min": 10, "avg": 10, "max": 10,
                            "p50": 10, "p95": 10, "p99": 10},
            "services": {"gw": 1}, "anomaly_counts": {},
        })
        self.assertIn("traces: 1", out)


if __name__ == "__main__":
    unittest.main()

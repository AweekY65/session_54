"""Tests for span parsing / validation."""

import unittest

from tracelet.model import Span, ModelError, parse_time_us


class TimeParsingTests(unittest.TestCase):
    def test_int_and_numeric_string(self):
        self.assertEqual(parse_time_us(123), 123)
        self.assertEqual(parse_time_us("1700000000000000"), 1700000000000000)
        self.assertEqual(parse_time_us(123.9), 123)

    def test_iso8601(self):
        self.assertEqual(parse_time_us("1970-01-01T00:00:00Z"), 0)
        self.assertEqual(parse_time_us("1970-01-01T00:00:01+00:00"), 1_000_000)

    def test_bad(self):
        with self.assertRaises(ModelError):
            parse_time_us("not-a-time")
        with self.assertRaises(ModelError):
            parse_time_us(None)


class SpanModelTests(unittest.TestCase):
    def _base(self, **over):
        kw = dict(trace_id="t", span_id="s", parent_id=None,
                  service="svc", name="n", start_us=0, end_us=10)
        kw.update(over)
        return Span(**kw)

    def test_required_fields(self):
        with self.assertRaises(ModelError):
            self._base(trace_id="")
        with self.assertRaises(ModelError):
            self._base(span_id=" ")
        with self.assertRaises(ModelError):
            self._base(service="")
        with self.assertRaises(ModelError):
            self._base(start_us=10, end_us=1)

    def test_empty_parent_normalised_to_none(self):
        self.assertIsNone(self._base(parent_id="").parent_id)

    def test_from_dict_aliases(self):
        s = Span.from_dict({
            "traceId": "t", "spanId": "s", "parentId": "p",
            "serviceName": "svc", "operation": "do",
            "start_time": 0, "end_time": 5,
            "status_code": "ERROR", "tags": {"k": "v"},
        })
        self.assertEqual((s.trace_id, s.span_id, s.parent_id), ("t", "s", "p"))
        self.assertEqual(s.service, "svc")
        self.assertEqual(s.name, "do")
        self.assertEqual(s.status, "ERROR")
        self.assertEqual(s.attributes, {"k": "v"})

    def test_roundtrip(self):
        s = self._base(parent_id="p", attributes={"a": 1})
        d = s.to_dict()
        self.assertEqual(Span.from_dict(d), s)


if __name__ == "__main__":
    unittest.main()

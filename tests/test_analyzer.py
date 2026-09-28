"""Unit tests for tree reconstruction and timing analysis."""

import unittest

from tracelet.analyzer import (
    build_trace,
    build_traces,
    merge_intervals,
    ORPHAN,
    DUPLICATE,
    CYCLE,
)
from tracelet.model import Span


def sp(span_id, parent, service, start, dur, *, tid="t1", name="op",
       status="OK", **attrs):
    return Span(trace_id=tid, span_id=span_id, parent_id=parent,
                service=service, name=name, start_us=start,
                end_us=start + dur, status=status, attributes=dict(attrs))


class MergeIntervalsTests(unittest.TestCase):
    def test_disjoint_nested_overlapping(self):
        self.assertEqual(merge_intervals([(0, 10), (10, 20)]),
                         [(0, 20)])           # touching merge
        self.assertEqual(merge_intervals([(0, 100), (10, 20), (30, 40)]),
                         [(0, 100)])           # nested
        self.assertEqual(merge_intervals([(0, 10), (5, 15), (20, 30)]),
                         [(0, 15), (20, 30)])  # partial overlap + disjoint
        self.assertEqual(merge_intervals([]), [])


class NormalTreeTests(unittest.TestCase):
    def setUp(self):
        # root 0..100 ; child-a 10..60 (a1 20..30, a2 40..50) ;
        # child-b 60..90
        self.spans = [
            sp("root", None, "gateway", 0, 100),
            sp("a", "root", "svc-a", 10, 50),
            sp("a1", "a", "svc-a", 20, 10),
            sp("a2", "a", "svc-a", 40, 10),
            sp("b", "root", "svc-b", 60, 30),
        ]
        self.trace = build_trace(self.spans)

    def test_structure(self):
        self.assertEqual(self.trace.span_count, 5)
        self.assertEqual([n.span.span_id for n in self.trace.roots], ["root"])
        root = self.trace.roots[0]
        self.assertEqual([c.span.span_id for c in root.children],
                         ["a", "b"])
        self.assertEqual(
            [c.span.span_id for c in
             self.trace.nodes["a"].children], ["a1", "a2"])

    def test_total_latency(self):
        self.assertEqual(self.trace.start_us, 0)
        self.assertEqual(self.trace.end_us, 100)
        self.assertEqual(self.trace.duration_us, 100)

    def test_self_time_excludes_children(self):
        n = self.trace.nodes
        # a: 50 own - 10 (a1) - 10 (a2) = 30
        self.assertEqual(n["a"].self_us, 30)
        self.assertEqual(n["a1"].self_us, 10)
        # root: 100 - union(a[10,60], b[60,90]) = 100 - 80 = 20
        self.assertEqual(n["root"].self_us, 20)
        self.assertEqual(n["b"].self_us, 30)

    def test_service_time_merges_nested_intervals(self):
        # svc-a spans: a[10,60), a1[20,30), a2[40,50) -> all inside a = 50
        self.assertEqual(self.trace.service_time_us["svc-a"], 50)
        self.assertEqual(self.trace.service_time_us["svc-b"], 30)
        self.assertEqual(self.trace.service_time_us["gateway"], 100)

    def test_critical_path_picks_longest_self_chain(self):
        # path weights (self times):
        #   root->a->a1 = 20+30+10=60 ; root->a->a2 = 60
        #   root->b     = 20+30=50
        ids = [s.span_id for s in self.trace.critical_path]
        self.assertEqual(ids[0], "root")
        self.assertEqual(ids[1], "a")
        # a1/a2 tie on weight (both 10) and a1 has the smaller id
        self.assertEqual(ids[2], "a1")
        td = self.trace.to_dict()
        self.assertEqual(td["critical_path_us"], 60)
        # wall-span of nested spans is larger, but must not be reported as
        # the critical path weight.
        self.assertGreater(td["critical_path_span_us"], 60)


class ParallelChildrenTests(unittest.TestCase):
    def test_overlapping_children_counted_once_in_self_time(self):
        # root 0..100, two parallel children overlapping 20..80
        trace = build_trace([
            sp("root", None, "gw", 0, 100),
            sp("p1", "root", "worker", 20, 60),
            sp("p2", "root", "worker", 30, 50),
        ])
        n = trace.nodes
        # children union = [20,80) = 60 -> root self = 40
        self.assertEqual(n["root"].self_us, 40)
        # same service, overlapping intervals merged: 60 (not 110)
        self.assertEqual(trace.service_time_us["worker"], 60)
        # two parallel leaves each weigh 60/50; p1 is heavier
        self.assertEqual([s.span_id for s in trace.critical_path],
                         ["root", "p1"])

    def test_gap_between_child_and_parent_counts_as_parent_self(self):
        trace = build_trace([
            sp("root", None, "gw", 0, 100),
            sp("c", "root", "svc", 20, 30),  # [20,50)
        ])
        self.assertEqual(trace.nodes["root"].self_us, 70)
        self.assertEqual([s.span_id for s in trace.critical_path],
                         ["root", "c"])

    def test_child_extending_past_parent_is_clipped(self):
        trace = build_trace([
            sp("root", None, "gw", 0, 50),
            sp("c", "root", "svc", 10, 100),  # [10,110) escapes parent
        ])
        # clipped to [10,50): root self = 10, never negative
        self.assertEqual(trace.nodes["root"].self_us, 10)
        # total latency follows actual span data (110)
        self.assertEqual(trace.duration_us, 110)


class OutOfOrderTests(unittest.TestCase):
    def test_parent_arrives_after_children(self):
        ordered = [
            sp("root", None, "gw", 0, 100),
            sp("c1", "root", "svc", 10, 20),
            sp("c2", "c1", "svc", 12, 5),
        ]
        for perm in (list(reversed(ordered)),
                     [ordered[2], ordered[0], ordered[1]]):
            trace = build_trace(perm)
            self.assertEqual(trace.span_count, 3)
            self.assertEqual([n.span.span_id for n in trace.roots], ["root"])
            self.assertEqual(
                [c.span.span_id for c in trace.nodes["root"].children],
                ["c1"])
            self.assertEqual(
                [c.span.span_id for c in trace.nodes["c1"].children],
                ["c2"])
            self.assertEqual(trace.anomalies, [])

    def test_separate_trace_groups(self):
        traces = build_traces([
            sp("x1", None, "s", 0, 5, tid="A"),
            sp("y1", None, "s", 0, 5, tid="B"),
            sp("x2", "x1", "s", 1, 2, tid="A"),
        ])
        self.assertEqual(set(traces), {"A", "B"})
        self.assertEqual(traces["A"].span_count, 2)


class OrphanTests(unittest.TestCase):
    def test_missing_parent_is_root_plus_anomaly(self):
        trace = build_trace([
            sp("root", None, "gw", 0, 100),
            sp("lost", "ghost", "svc", 10, 20),
        ])
        ids = sorted(n.span.span_id for n in trace.roots)
        self.assertEqual(ids, ["lost", "root"])
        kinds = [a.kind for a in trace.anomalies]
        self.assertEqual(kinds, [ORPHAN])
        self.assertEqual(trace.anomalies[0].span_id, "lost")
        self.assertIn("ghost", trace.anomalies[0].message)


class DuplicateTests(unittest.TestCase):
    def test_duplicate_span_id_flagged_first_kept(self):
        trace = build_trace([
            sp("root", None, "gw", 0, 100),
            sp("d", "root", "svc-a", 10, 10),
            sp("d", "root", "svc-b", 20, 30),  # duplicate id, later report
        ])
        self.assertEqual(trace.span_count, 2)  # only 2 unique spans
        dup = [a for a in trace.anomalies if a.kind == DUPLICATE]
        self.assertEqual(len(dup), 1)
        self.assertEqual(dup[0].span_id, "d")
        # first occurrence is retained
        self.assertEqual(trace.nodes["d"].span.service, "svc-a")


class CycleTests(unittest.TestCase):
    def test_two_node_cycle_severed_and_flagged(self):
        trace = build_trace([
            sp("a", "b", "svc", 0, 10),
            sp("b", "a", "svc", 5, 10),
        ])
        cyc = [x for x in trace.anomalies if x.kind == CYCLE]
        self.assertEqual(len(cyc), 1)
        self.assertIn("a -> b -> a", cyc[0].message)
        # exactly one root survives; all spans still reachable
        self.assertEqual(len(trace.roots), 1)
        seen: list[str] = []

        def walk(node):
            seen.append(node.span.span_id)
            for c in node.children:
                walk(c)

        for r in trace.roots:
            walk(r)
        self.assertEqual(sorted(seen), ["a", "b"])

    def test_three_node_cycle(self):
        trace = build_trace([
            sp("a", "c", "s", 0, 10),
            sp("b", "a", "s", 0, 10),
            sp("c", "b", "s", 0, 10),
        ])
        self.assertEqual(len([x for x in trace.anomalies
                              if x.kind == CYCLE]), 1)
        self.assertEqual(len(trace.roots), 1)

    def test_self_loop(self):
        trace = build_trace([sp("x", "x", "s", 0, 10)])
        self.assertEqual(len([x for x in trace.anomalies
                              if x.kind == CYCLE]), 1)
        self.assertEqual([n.span.span_id for n in trace.roots], ["x"])

    def test_cycle_with_node_feeding_into_it(self):
        # Functional graph (edges = parent pointers):
        #   root -> None ; x -> y ; y -> z ; z -> y
        # y/z form a cycle; x feeds into it from outside.
        trace = build_trace([
            sp("root", None, "gw", 0, 100),
            Span("t1", "x", "y", "s", "op", 10, 20),
            Span("t1", "y", "z", "s", "op", 20, 30),
            Span("t1", "z", "y", "s", "op", 25, 40),
        ])
        cyc = [x for x in trace.anomalies if x.kind == CYCLE]
        self.assertEqual(len(cyc), 1)
        # back-edge z->y severed: roots are "root" and "z"; every span is
        # still reachable (x -> y -> z chain, plus root)
        reachable: list[str] = []

        def walk(node):
            reachable.append(node.span.span_id)
            for ch in node.children:
                walk(ch)

        for r in trace.roots:
            walk(r)
        self.assertEqual(sorted(reachable), ["root", "x", "y", "z"])


class ErrorStatusTests(unittest.TestCase):
    def test_trace_error_if_any_span_errors(self):
        trace = build_trace([
            sp("root", None, "gw", 0, 10, status="ERROR"),
            sp("ok", "root", "s", 1, 2, status="OK"),
        ])
        self.assertTrue(trace.is_error())
        self.assertEqual(trace.status, "ERROR")
        self.assertEqual(trace.error_count, 1)


if __name__ == "__main__":
    unittest.main()

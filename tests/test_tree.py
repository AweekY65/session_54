"""Tree reconstruction: normal tree, out-of-order, orphan, dup, cycle."""

import pytest

from conftest import make_span
from localtrace.tree import build_trace_tree


def test_normal_tree(simple_spans):
    tree = build_trace_tree(simple_spans)
    assert tree.roots == ["root"]
    assert tree.children["root"] == ["c1", "c2"]
    assert tree.children["c1"] == ["gc"]
    # pre-order: parent before child
    assert tree.order.index("root") < tree.order.index("c1")
    assert tree.order.index("c1") < tree.order.index("gc")
    assert tree.anomalies == []


def test_out_of_order_arrival(simple_spans):
    """Children arrive long before their parent: links must not be lost."""
    shuffled = list(reversed(simple_spans))  # gc, c2, c1, root
    tree = build_trace_tree(shuffled)
    assert tree.roots == ["root"]
    assert set(tree.children["root"]) == {"c1", "c2"}
    assert tree.children["c1"] == ["gc"]
    assert tree.order == ["root", "c1", "gc", "c2"]
    assert tree.anomalies == []


def test_orphan_span_attached_as_root():
    spans = [
        make_span(span_id="a", parent_id="missing", service="svc",
                 start=0, end=10),
        make_span(span_id="b", parent_id="", service="svc",
                 start=1, end=9),
    ]
    tree = build_trace_tree(spans)
    assert set(tree.roots) == {"a", "b"}
    kinds = [x.kind for x in tree.anomalies]
    assert kinds == ["orphan_span"]
    anomaly = tree.anomalies[0]
    assert anomaly.span_ids == ["a"]
    assert "missing" in anomaly.detail
    # orphan is still traversable
    assert "a" in tree.order


def test_duplicate_span_id_first_wins():
    first = make_span(span_id="a", service="svc-a", start=0, end=5)
    dup = make_span(span_id="a", service="svc-DIFFERENT", start=0, end=5)
    tree = build_trace_tree([first, dup])
    assert tree.spans["a"].service == "svc-a"
    assert [x.kind for x in tree.anomalies] == ["duplicate_span_id"]


def test_self_parent_is_cycle():
    spans = [make_span(span_id="a", parent_id="a", start=0, end=5)]
    tree = build_trace_tree(spans)
    assert [x.kind for x in tree.anomalies] == ["cycle"]
    assert tree.anomalies[0].span_ids == ["a"]
    assert tree.roots == ["a"]
    assert tree.order == ["a"]


def test_three_node_cycle_detected_and_cut():
    spans = [
        make_span(span_id="a", parent_id="b", start=0, end=10),
        make_span(span_id="b", parent_id="c", start=1, end=9),
        make_span(span_id="c", parent_id="a", start=2, end=8),
    ]
    tree = build_trace_tree(spans)
    cycles = [x for x in tree.anomalies if x.kind == "cycle"]
    assert len(cycles) == 1
    # canonical loop starts at smallest id
    assert cycles[0].span_ids == ["a", "b", "c"]
    # after cutting one edge, every node is reachable exactly once
    assert sorted(tree.order) == ["a", "b", "c"]
    assert len(tree.order) == len(set(tree.order))


def test_pred_node_leading_into_cycle():
    # p -> a -> b -> a ; p leads into the cycle but is not on it
    spans = [
        make_span(span_id="p", parent_id="a", start=0, end=10),
        make_span(span_id="a", parent_id="b", start=1, end=9),
        make_span(span_id="b", parent_id="a", start=2, end=8),
    ]
    tree = build_trace_tree(spans)
    assert len([x for x in tree.anomalies if x.kind == "cycle"]) == 1
    assert sorted(tree.order) == ["a", "b", "p"]


def test_multiple_traces_rejected():
    with pytest.raises(ValueError):
        build_trace_tree([
            make_span(trace_id="t1"),
            make_span(trace_id="t2"),
        ])

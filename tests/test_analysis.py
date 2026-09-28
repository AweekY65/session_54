"""Analysis: latency, exclusive service time, parallel children, crit path."""

from conftest import make_span
from localtrace.analysis import analyze_trace, exclusive_intervals, merge_intervals
from localtrace.model import Status


def test_trace_latency_and_status(simple_spans):
    a = analyze_trace(simple_spans)
    assert a.duration_us == 100
    assert a.status is Status.OK
    assert a.span_count == 4


def test_error_rollup():
    spans = [
        make_span(span_id="r", start=0, end=10),
        make_span(span_id="c", parent_id="r", start=1, end=9,
                  status=Status.ERROR),
    ]
    a = analyze_trace(spans)
    assert a.status is Status.ERROR
    assert a.error_count == 1


def test_exclusive_time_nested_not_double_counted(simple_spans):
    a = analyze_trace(simple_spans)
    ex = a.span_exclusive_us
    # gc: 40, c1 spans 10..80 with gc 20..60 inside -> 30
    assert ex["gc"] == 40
    assert ex["c1"] == (80 - 10) - (60 - 20)
    assert ex["c2"] == 10
    # root: 100 - union(c1 10..80, c2 85..95) = 100 - (70+10) = 20
    assert ex["root"] == 20
    # no parallel siblings here -> exclusive sum equals the root window
    assert sum(ex.values()) == 100


def test_service_exclusive_and_inclusive():
    a = analyze_trace([
        make_span(span_id="r", service="a", start=0, end=100),
        make_span(span_id="c", parent_id="r", service="b", start=10, end=90),
    ])
    assert a.service_time_us == {"a": 20, "b": 80}
    # inclusive sums each service's wall spans; nesting is visible as >100 total
    assert a.service_inclusive_us == {"a": 100, "b": 80}


def test_parallel_children_overlap_merged():
    """Two parallel children of root overlap; union subtracts overlap once."""
    # root 0..100, children 10..60 and 40..90 (overlap 40..60)
    spans = [
        make_span(span_id="r", start=0, end=100),
        make_span(span_id="p1", parent_id="r", service="b", start=10, end=60),
        make_span(span_id="p2", parent_id="r", service="c", start=40, end=90),
    ]
    a = analyze_trace(spans)
    covered = merge_intervals([(10, 60), (40, 90)])
    assert covered == [(10, 90)]
    assert a.span_exclusive_us["r"] == 100 - 80  # 20, not 30
    # per-service exclusive totals: root 20, p1 50, p2 50 (parallel work
    # appears once per service), busy time counted across services
    assert a.service_time_us == {"svc": 20, "b": 50, "c": 50}


def test_exclusive_intervals_partial_child_overflow():
    parent = make_span(span_id="r", start=10, end=90)
    child = make_span(span_id="c", parent_id="r", start=0, end=100)
    parts = exclusive_intervals(parent, [child])
    # child (clipped to parent) covers parent completely
    assert parts == []


def test_critical_path_chain(simple_spans):
    a = analyze_trace(simple_spans)
    cp = a.critical_path
    # c1 ends at 80 > c2 end 95? 80 < 95 -> c2 ends later -> path root -> c2
    assert cp.span_ids == ["root", "c2"]
    assert cp.total_us == 100
    assert cp.parallel_time_us >= 0


def test_critical_path_picks_latest_finishing_child():
    spans = [
        make_span(span_id="r", start=0, end=100),
        make_span(span_id="short", parent_id="r", service="b",
                  start=10, end=30),
        make_span(span_id="long", parent_id="r", service="c",
                  start=20, end=95),
        make_span(span_id="tail", parent_id="long", service="d",
                  start=40, end=90),
    ]
    a = analyze_trace(spans)
    assert a.critical_path.span_ids == ["r", "long", "tail"]
    node_by_id = {n["span_id"]: n for n in a.critical_path.nodes}
    # r exclusive = 100 - union(short 10..30, long 20..95) = 100 - 85 = 15
    assert node_by_id["r"]["contribution_us"] == 15
    # long exclusive = 75 - tail(50) = 25
    assert node_by_id["long"]["contribution_us"] == 25
    assert node_by_id["tail"]["contribution_us"] == 50
    contrib_sum = sum(n["contribution_us"] for n in a.critical_path.nodes)
    assert contrib_sum == 90
    assert a.critical_path.parallel_time_us == 100 - 90


def test_critical_path_parallel_branch_not_double_counted():
    # chain r->a, sibling b runs in parallel; sum(contrib) + parallel = total
    spans = [
        make_span(span_id="r", start=0, end=100),
        make_span(span_id="a", parent_id="r", service="a", start=10, end=80),
        make_span(span_id="b", parent_id="r", service="b", start=15, end=70),
    ]
    a = analyze_trace(spans)
    cp = a.critical_path
    assert cp.span_ids == ["r", "a"]
    assert (sum(n["contribution_us"] for n in cp.nodes)
            + cp.parallel_time_us) == a.duration_us


def test_empty_trace_safe():
    from localtrace.tree import build_trace_tree
    a = analyze_trace([])
    assert a.duration_us == 0
    assert a.critical_path.span_ids == []
    assert a.span_count == 0

"""Trace analytics: latency, per-service time and the critical path.

Nested-span time is *never* summed naively. Two metrics are produced:

inclusive time (wall time)
    A span's own ``end - start``. Children are contained in it.

exclusive time (self time)
    The span's wall time minus the union of its *direct* children's
    intervals. Siblings that overlap are first merged, so parallel children
    cannot subtract the same microseconds twice. A parent's exclusive time
    plus the union of its children's time exactly covers the parent's wall
    time; per-service exclusive totals are therefore free of nesting
    double-counting.

Critical path
    The chain of spans that determines the trace duration: starting at the
    root that ends latest, repeatedly descend into the child whose *end* is
    latest (the child that was still running when the parent finished).
    Each node contributes its exclusive time, clipped to the root span's
    window; because the chain spans are nested, exclusive times of chain
    nodes count work *outside* every chain descendant, so contributions
    never overlap. ``parallel_time_us`` is the rest of the root's wall time:
    work on sibling branches plus idle gaps between chain segments.

Caveat on summing exclusive time
    Exclusive time partitions the busy time of one parent's subtree level;
    summing exclusive time over *all* spans equals the total busy time plus
    time spent in overlapping parallel siblings, and can therefore exceed
    the trace duration when children run concurrently. Per-service and
    critical-path numbers are the non-double-counted metrics.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import Span, Status
from .tree import BuiltTree, build_trace_tree


@dataclass
class CriticalPath:
    span_ids: list[str]
    nodes: list[dict]
    total_us: int
    parallel_time_us: int

    def to_dict(self) -> dict:
        return {
            "span_ids": list(self.span_ids),
            "nodes": list(self.nodes),
            "total_us": self.total_us,
            "parallel_time_us": self.parallel_time_us,
        }


@dataclass
class TraceAnalysis:
    trace_id: str
    tree: BuiltTree
    start_us: int
    end_us: int
    duration_us: int
    span_count: int
    error_count: int
    status: Status
    services: list[str]
    service_time_us: dict[str, int]
    service_inclusive_us: dict[str, int]
    span_exclusive_us: dict[str, int]
    critical_path: CriticalPath
    anomalies: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "start_us": self.start_us,
            "end_us": self.end_us,
            "duration_us": self.duration_us,
            "span_count": self.span_count,
            "error_count": self.error_count,
            "status": self.status.value,
            "services": list(self.services),
            "service_exclusive_us": {
                s: self.service_time_us[s] for s in self.services
            },
            "service_inclusive_us": {
                s: self.service_inclusive_us[s] for s in self.services
            },
            "span_exclusive_us": dict(sorted(self.span_exclusive_us.items())),
            "critical_path": self.critical_path.to_dict(),
            "anomalies": [a.to_dict() for a in self.anomalies],
        }


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union of half-open [start, end) integer intervals, sorted."""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: list[list[int]] = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:  # touching intervals merge
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def exclusive_intervals(span: Span, children: list[Span]) -> list[tuple[int, int]]:
    """Parts of ``span`` not covered by the union of direct children."""
    covered = merge_intervals([(c.start_us, c.end_us) for c in children])
    result: list[tuple[int, int]] = []
    cursor = span.start_us
    for cstart, cend in covered:
        cstart = max(cstart, span.start_us)
        cend = min(cend, span.end_us)
        if cstart > cursor:
            result.append((cursor, cstart))
        if cend > cursor:
            cursor = cend
    if cursor < span.end_us:
        result.append((cursor, span.end_us))
    return result


def _critical_path(tree: BuiltTree,
                   exclusive: dict[str, int]) -> CriticalPath:
    if not tree.spans:
        return CriticalPath([], [], 0, 0)

    # Root that ends latest; ties broken by earliest start then id.
    root = min(
        tree.roots,
        key=lambda sid: (-tree.spans[sid].end_us,
                         tree.spans[sid].start_us, sid),
    )

    chain: list[str] = []
    cur: str | None = root
    while cur is not None:
        chain.append(cur)
        kids = tree.children.get(cur, ())
        if not kids:
            break
        # Child running at the parent's finish; ties: earliest start then id.
        cur = min(
            kids,
            key=lambda sid: (-tree.spans[sid].end_us,
                             tree.spans[sid].start_us, sid),
        )

    # Each chain node contributes its exclusive intervals clipped to the
    # root's window. Exclusive time of a chain span excludes ALL its direct
    # children (including the next chain node), so work is attributed to
    # exactly one chain level: contributions are disjoint.
    root_span = tree.spans[root]
    nodes: list[dict] = []
    clipped_total = 0
    for sid in chain:
        span = tree.spans[sid]
        raw = exclusive.get(sid, 0)
        clipped = _exclusive_in_window(sid, tree,
                                       root_span.start_us, root_span.end_us)
        clipped_total += clipped
        nodes.append({
            "span_id": sid,
            "service": span.service,
            "name": span.name,
            "start_us": span.start_us,
            "end_us": span.end_us,
            "duration_us": span.duration_us,
            "exclusive_us": raw,
            "contribution_us": clipped,
        })

    total = root_span.end_us - root_span.start_us
    parallel = max(0, total - clipped_total)
    return CriticalPath(
        span_ids=chain,
        nodes=nodes,
        total_us=total,
        parallel_time_us=parallel,
    )


def _exclusive_in_window(sid: str, tree: BuiltTree,
                         window_start: int, window_end: int) -> int:
    """Exclusive microseconds of span ``sid`` that fall inside [w_start, w_end)."""
    span = tree.spans[sid]
    children = [tree.spans[c] for c in tree.children.get(sid, ())]
    amount = 0
    for s, e in exclusive_intervals(span, children):
        s = max(s, window_start)
        e = min(e, window_end)
        if e > s:
            amount += e - s
    return amount


def analyze_trace(spans) -> TraceAnalysis:
    """Build the tree and compute latency, service times and critical path."""
    tree = build_trace_tree(spans)
    return analyze_tree(tree)


def analyze_tree(tree: BuiltTree) -> TraceAnalysis:
    spans = tree.spans

    exclusive: dict[str, int] = {}
    service_exclusive: dict[str, int] = {}
    service_inclusive: dict[str, int] = {}
    error_count = 0

    for sid in tree.order:
        span = spans[sid]
        children = [spans[c] for c in tree.children.get(sid, ())]
        ex = sum(e - s for s, e in exclusive_intervals(span, children))
        exclusive[sid] = ex
        service_exclusive[span.service] = service_exclusive.get(span.service, 0) + ex
        service_inclusive[span.service] = (
            service_inclusive.get(span.service, 0) + span.duration_us
        )
        if span.status == Status.ERROR:
            error_count += 1

    if spans:
        start_us = min(s.start_us for s in spans.values())
        end_us = max(s.end_us for s in spans.values())
    else:
        start_us = end_us = 0

    if not spans:
        status = Status.UNSET
    elif error_count:
        status = Status.ERROR
    elif all(s.status == Status.UNSET for s in spans.values()):
        status = Status.UNSET
    else:
        status = Status.OK

    services = sorted(service_exclusive)
    cp = _critical_path(tree, exclusive)

    return TraceAnalysis(
        trace_id=tree.trace_id,
        tree=tree,
        start_us=start_us,
        end_us=end_us,
        duration_us=end_us - start_us,
        span_count=len(spans),
        error_count=error_count,
        status=status,
        services=services,
        service_time_us={s: service_exclusive.get(s, 0) for s in services},
        service_inclusive_us={
            s: service_inclusive.get(s, 0) for s in services
        },
        span_exclusive_us=exclusive,
        critical_path=cp,
        anomalies=list(tree.anomalies),
    )

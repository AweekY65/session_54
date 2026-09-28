"""Trace-tree reconstruction and analysis.

The analyzer is a pure function over spans; it never touches storage and is
happy to receive spans in any order.  For each trace it:

1. rebuilds the parent/child tree (late-arriving parents are linked up too,
   because both sides are already present when analysis runs);
2. flags anomalies:
     * orphan span        - parent id points at a span that does not exist
     * duplicate span id  - the same (trace_id, span_id) reported twice
     * cycle              - parent pointers form a directed cycle;
                            the back-edge that closes a cycle is severed so a
                            usable tree is still produced;
3. computes total latency, per-service time (nested/overlapping spans are
   merged on a timeline, never double counted), per-span self time and the
   critical path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .model import Span, _ERROR

# Anomaly kinds (stable string tokens used in JSON reports).
ORPHAN = "orphan_span"
DUPLICATE = "duplicate_span_id"
CYCLE = "cycle"

# Anomaly severity, for deterministic report ordering.
_SEV = {CYCLE: 0, DUPLICATE: 1, ORPHAN: 2}


@dataclass(frozen=True)
class Anomaly:
    kind: str
    span_id: Optional[str]
    message: str
    trace_id: Optional[str] = None
    severity: int = 2

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "span_id": self.span_id,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass
class TraceNode:
    span: Span
    children: list["TraceNode"] = field(default_factory=list)
    self_us: int = 0
    # Longest root->leaf weighted (self time) path from this node downward.
    crit_to_leaf_us: int = 0
    # Child chosen on the critical path (None for leaves / when all children
    # contribute zero; tie-break is deterministic on span_id).
    crit_child: Optional["TraceNode"] = None

    def to_dict(self) -> dict:
        return {
            "span": self.span.to_dict(),
            "self_us": self.self_us,
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class Trace:
    trace_id: str
    roots: list[TraceNode]
    nodes: dict[str, TraceNode]
    start_us: int
    end_us: int
    duration_us: int
    service_time_us: dict[str, int]
    status: str
    error_count: int
    span_count: int
    anomalies: list[Anomaly]
    critical_path: list[Span]

    def is_error(self) -> bool:
        return self.status == _ERROR

    def to_dict(self) -> dict:
        crit_ids = {s.span_id for s in self.critical_path}
        crit_self_us = sum(self.nodes[i].self_us for i in crit_ids
                           if i in self.nodes)
        return {
            "trace_id": self.trace_id,
            "start_us": self.start_us,
            "end_us": self.end_us,
            "duration_us": self.duration_us,
            "status": self.status,
            "error_count": self.error_count,
            "span_count": self.span_count,
            "service_time_us": dict(sorted(self.service_time_us.items())),
            "critical_path": [s.to_dict() for s in self.critical_path],
            # Weight of the critical path = sum of self time on it
            # (nested time is NOT accumulated), plus the wall-clock span of
            # the path for reference.
            "critical_path_us": crit_self_us,
            "critical_path_span_us": sum(
                s.end_us - s.start_us for s in self.critical_path),
            "roots": [r.to_dict() for r in self.roots],
            "anomalies": [a.to_dict() for a in self.anomalies],
        }


def merge_intervals(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping/touching [start, end) intervals into disjoint ones."""
    ordered = sorted(intervals)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if end < start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        elif end > merged[-1][1]:
            merged[-1] = (merged[-1][0], end)
    return merged


def _sort_anomalies(anoms: Iterable[Anomaly]) -> list[Anomaly]:
    return sorted(anoms, key=lambda a: (a.severity, a.kind,
                                        a.span_id or "", a.message))


def _build_one(trace_id: str, spans: Sequence[Span]) -> Trace:
    """Reconstruct a single trace from its spans (any arrival order)."""
    anomalies: list[Anomaly] = []

    # --- duplicate detection: first occurrence wins, rest are anomalies ---
    by_id: dict[str, Span] = {}
    for sp in spans:
        if sp.span_id in by_id:
            anomalies.append(Anomaly(
                kind=DUPLICATE,
                span_id=sp.span_id,
                severity=_SEV[DUPLICATE],
                message=(f"duplicate span id {sp.span_id!r} "
                         f"(service={sp.service}, name={sp.name!r}); "
                         f"keeping the first received occurrence"),
            ))
        else:
            by_id[sp.span_id] = sp

    # --- functional parent graph (each node points at <=1 parent) ----------
    parent_of: dict[str, Optional[str]] = {}
    for sid, sp in by_id.items():
        pid = sp.parent_id
        if pid is None:
            parent_of[sid] = None
        elif pid not in by_id:
            # Missing parent. Keep no parent so the span is still reachable
            # as a root; the anomaly records the broken link.
            parent_of[sid] = None
            anomalies.append(Anomaly(
                kind=ORPHAN,
                span_id=sid,
                severity=_SEV[ORPHAN],
                message=(f"span {sid!r} ({sp.service}/{sp.name}) references "
                         f"unknown parent {pid!r}"),
            ))
        elif pid == sid:
            # Self loop: the generic walk below detects and severs it, but
            # give a clearer message up front.
            parent_of[sid] = sid
        else:
            parent_of[sid] = pid

    # --- cycle detection ----------------------------------------------------
    # Every node has out-degree <= 1 (one parent pointer), so follow the
    # chain from each not-yet-resolved node. Re-entering a node on the
    # current chain means a directed cycle; sever exactly the back-edge that
    # closes it (one anomaly per distinct cycle).
    resolved: set[str] = set()
    for start in sorted(by_id):
        if start in resolved:
            continue
        path: list[str] = []
        pos: dict[str, int] = {}
        node: Optional[str] = start
        while node is not None and node not in resolved:
            if node in pos:
                idx = pos[node]
                cyc = path[idx:] + [node]
                closed_by = path[-1]
                if node == closed_by:
                    message = f"span {node!r} lists itself as parent"
                else:
                    message = ("parent chain forms a cycle: "
                               + " -> ".join(cyc))
                anomalies.append(Anomaly(
                    kind=CYCLE,
                    span_id=closed_by,
                    severity=_SEV[CYCLE],
                    message=message,
                ))
                parent_of[closed_by] = None  # sever the back-edge
                break
            pos[node] = len(path)
            path.append(node)
            node = parent_of[node]
        resolved.update(path)

    # --- assemble the tree --------------------------------------------------
    nodes = {sid: TraceNode(span=sp) for sid, sp in by_id.items()}
    roots: list[TraceNode] = []
    for sid, node in nodes.items():
        pid = parent_of[sid]
        if pid is None:
            roots.append(node)
        else:
            nodes[pid].children.append(node)

    # Deterministic child / root ordering: start time, then span id.
    for node in nodes.values():
        node.children.sort(key=lambda n: (n.span.start_us,
                                          n.span.span_id))
    roots.sort(key=lambda n: (n.span.start_us, n.span.span_id))

    # --- timings ------------------------------------------------------------
    accepted = list(by_id.values())
    start_us = min(s.start_us for s in accepted)
    end_us = max(s.end_us for s in accepted)

    # Per-service time: union of that service's span intervals, so nested or
    # parallel same-service spans are counted once (no naive accumulation).
    by_service: dict[str, list[tuple[int, int]]] = {}
    for sp in accepted:
        by_service.setdefault(sp.service, []).append((sp.start_us, sp.end_us))
    service_time_us = {
        svc: sum(e - s for s, e in merge_intervals(ivs))
        for svc, ivs in by_service.items()
    }

    # Self time: own duration minus the union of child intervals clipped to
    # the span's own interval (clipped because children may escape a parent
    # when reporting clocks are skewed / data is malformed).
    order: list[TraceNode] = []
    seen: set[str] = set()

    def post_order(root: TraceNode) -> None:
        stack = [root]
        while stack:
            n = stack.pop()
            if n.span.span_id in seen:
                continue
            seen.add(n.span.span_id)
            order.append(n)
            stack.extend(n.children)

    for r in roots:
        post_order(r)
    for node in order:
        sp = node.span
        child_ivs = [
            (max(c.span.start_us, sp.start_us),
             min(c.span.end_us, sp.end_us))
            for c in node.children
        ]
        covered = sum(e - s for s, e in merge_intervals(child_ivs) if e > s)
        node.self_us = max(0, sp.duration_us - covered)

    # --- critical path ------------------------------------------------------
    # Longest root->leaf path where node weight is self time.  Bottom-up over
    # the post-order sequence (children are always appended after parents, so
    # reverse it).
    for node in reversed(order):
        best = 0
        best_child: Optional[TraceNode] = None
        for ch in node.children:
            cand = ch.crit_to_leaf_us
            if cand > best or (cand == best and cand > 0 and (
                    best_child is None
                    or ch.span.span_id < best_child.span.span_id)):
                best = cand
                best_child = ch
        node.crit_to_leaf_us = node.self_us + best
        node.crit_child = best_child

    critical_path: list[Span] = []
    if roots:
        # Deterministic root: greatest weighted path, then earliest start,
        # then smallest span id.
        best_total = max(n.crit_to_leaf_us for n in roots)
        start_node = sorted(
            (n for n in roots if n.crit_to_leaf_us == best_total),
            key=lambda n: (n.span.start_us, n.span.span_id),
        )[0]
        cur: Optional[TraceNode] = start_node
        while cur is not None:
            critical_path.append(cur.span)
            cur = cur.crit_child

    error_count = sum(1 for s in accepted if s.status == _ERROR)
    anomalies = _sort_anomalies(anomalies)
    return Trace(
        trace_id=trace_id,
        roots=roots,
        nodes=nodes,
        start_us=start_us,
        end_us=end_us,
        duration_us=end_us - start_us,
        service_time_us=service_time_us,
        status=_ERROR if error_count else "OK",
        error_count=error_count,
        span_count=len(accepted),
        anomalies=anomalies,
        critical_path=critical_path,
    )


def build_traces(spans: Iterable[Span]) -> dict[str, Trace]:
    """Group spans by trace_id and reconstruct every trace.

    Returns an ordered mapping (insertion order = first-seen trace order).
    """
    groups: dict[str, list[Span]] = {}
    for sp in spans:
        groups.setdefault(sp.trace_id, []).append(sp)
    return {tid: _build_one(tid, group) for tid, group in groups.items()}


def build_trace(spans: Sequence[Span]) -> Trace:
    """Convenience helper: all spans must share one trace id."""
    spans = list(spans)
    if not spans:
        raise ValueError("cannot build a trace from zero spans")
    tids = {s.trace_id for s in spans}
    if len(tids) > 1:
        raise ValueError(f"mixed trace ids: {sorted(tids)}")
    return _build_one(spans[0].trace_id, spans)

"""Trace-tree reconstruction and structural anomaly detection.

Works purely in memory over an iterable of :class:`~localtrace.model.Span`,
so out-of-order arrival is handled naturally: feed spans in *any* order,
including children before their parents.

Detected anomalies
------------------
``duplicate_span_id``
    Two spans in the same trace share a span_id. The first occurrence wins
    (and the duplicate's span id is listed so nothing is silently lost).

``orphan_span``
    A non-root span references a parent_id that is absent from the trace.
    Orphans are still attached to the forest as extra roots so they remain
    queryable.

``cycle``
    Following parent pointers loops forever (A->B->C->A). Exactly one edge
    of the loop is cut so analysis can still proceed; the reported anomaly
    lists the rotated node ids in loop order.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import Span


@dataclass
class Anomaly:
    kind: str  # duplicate_span_id | orphan_span | cycle
    span_ids: list[str]
    detail: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "span_ids": list(self.span_ids),
                "detail": self.detail}


@dataclass
class BuiltTree:
    """A reconstructed trace forest.

    Attributes:
        trace_id: trace these spans belong to.
        spans: winning span per span_id (first occurrence kept).
        children: parent_id -> sorted child span ids.
        roots: span ids with no parent; deterministic order.
        order: pre-order DFS traversal from every root (parent before child).
        anomalies: structural problems found while building.
    """

    trace_id: str
    spans: dict[str, Span] = field(default_factory=dict)
    children: dict[str, list[str]] = field(default_factory=dict)
    roots: list[str] = field(default_factory=list)
    order: list[str] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "roots": list(self.roots),
            "children": {k: list(v) for k, v in sorted(self.children.items())},
            "order": list(self.order),
            "anomalies": [a.to_dict() for a in self.anomalies],
        }


def build_trace_tree(spans) -> BuiltTree:
    """Rebuild one trace's parent/child forest from spans in any order."""
    span_list = list(spans)
    trace_ids = {s.trace_id for s in span_list}
    if len(trace_ids) > 1:
        raise ValueError(
            f"build_trace_tree expects one trace, got {sorted(trace_ids)}"
        )
    trace_id = next(iter(trace_ids)) if trace_ids else ""
    tree = BuiltTree(trace_id=trace_id)

    # 1) Deduplicate: first occurrence wins, stable by arrival order.
    duplicates: list[str] = []
    for span in span_list:
        if span.span_id in tree.spans:
            duplicates.append(span.span_id)
            continue
        tree.spans[span.span_id] = span
    for dup_id in sorted(set(duplicates)):
        tree.anomalies.append(Anomaly(
            kind="duplicate_span_id",
            span_ids=[dup_id],
            detail="duplicate span_id; kept the first span received",
        ))

    # 2) Build adjacency, recording orphan references (parent absent).
    children: dict[str, list[str]] = {}
    declared_roots: list[str] = []
    orphans: list[str] = []
    for sid, span in tree.spans.items():
        if span.is_root:
            declared_roots.append(sid)
            continue
        if span.parent_id not in tree.spans:
            orphans.append(sid)
            continue
        children.setdefault(span.parent_id, []).append(sid)
    for child_list in children.values():
        child_list.sort()

    # 3) Cycle detection with memoised parent-chain walks.
    #    outcome maps sid -> ("cycle", loop_tuple) for nodes on/in a loop,
    #                       ("pred", loop_tuple) for nodes leading into one,
    #                       ("ok", None) for acyclic chains.
    outcome: dict[str, tuple[str, tuple[str, ...] | None]] = {}

    def walk(start: str) -> tuple[str, tuple[str, ...] | None]:
        if start in outcome:
            return outcome[start]
        path: list[str] = []
        index: dict[str, int] = {}
        cur = start
        while True:
            if cur in outcome:
                kind, loop = outcome[cur]
                result: tuple[str, tuple[str, ...] | None]
                if kind == "ok":
                    result = ("ok", None)
                else:  # cur is on the loop or already leads into it
                    result = ("pred", loop)
                for node in path:
                    outcome[node] = result
                return result
            if cur in index:
                raw_loop = path[index[cur]:]
                # Canonical rotation: start at the lexicographically smallest
                # id so the same loop reports identically regardless of entry.
                k = min(range(len(raw_loop)), key=lambda i: raw_loop[i])
                loop = tuple(raw_loop[k:] + raw_loop[:k])
                on_loop = set(loop)
                for node in path:
                    outcome[node] = (
                        ("cycle", loop) if node in on_loop else ("pred", loop)
                    )
                return outcome[start]
            index[cur] = len(path)
            path.append(cur)
            span = tree.spans.get(cur)
            if span is None or span.is_root:
                for node in path:
                    outcome[node] = ("ok", None)
                return outcome[start]
            nxt = span.parent_id
            if nxt == cur:  # self-parent: span itself is both present & root-like
                loop = (cur,)
                outcome[cur] = ("cycle", loop)
                for node in path[:-1]:
                    outcome[node] = ("pred", loop)
                return outcome[start]
            cur = nxt

    for sid in tree.spans:
        walk(sid)

    reported_loops: set[tuple[str, ...]] = set()
    for sid in sorted(tree.spans):
        kind, loop = outcome[sid]
        if kind == "cycle" and loop and loop not in reported_loops:
            reported_loops.add(loop)
            tree.anomalies.append(Anomaly(
                kind="cycle",
                span_ids=list(loop),
                detail="parent chain forms a cycle: "
                       + " -> ".join(list(loop) + [loop[0]]),
            ))

    # 4) Cut one edge per loop so the graph becomes a forest. Cut the edge
    #    entering the canonical smallest loop node (its parent pointer).
    cut_children_edges: set[tuple[str, str]] = set()
    cut_node_as_child: set[str] = set()
    for loop in reported_loops:
        smallest = loop[0]
        smallest_span = tree.spans[smallest]
        cut_children_edges.add((smallest_span.parent_id, smallest))
        cut_node_as_child.add(smallest)

    if cut_children_edges:
        children = {
            parent: [c for c in kids if (parent, c) not in cut_children_edges]
            for parent, kids in children.items()
        }
        children = {p: v for p, v in children.items() if v}

    for oid in sorted(orphans):
        tree.anomalies.append(Anomaly(
            kind="orphan_span",
            span_ids=[oid],
            detail=(f"span references missing parent "
                    f"{tree.spans[oid].parent_id!r}; attached as a root"),
        ))

    # 5) Roots = explicit roots + orphans + cycle-cut nodes.
    root_set: set[str] = set(declared_roots)
    root_set.update(orphans)
    root_set.update(cut_node_as_child)
    # Defensive: any node not reachable as someone's child is a root.
    has_parent = {c for kids in children.values() for c in kids}
    root_set.update(sid for sid in tree.spans if sid not in has_parent)
    roots = sorted(
        root_set,
        key=lambda sid: (tree.spans[sid].start_us, sid),
    )

    tree.children = children
    tree.roots = roots

    # 6) Deterministic pre-order traversal. Cycles are gone, so plain DFS.
    visited: set[str] = set()
    for root in roots:
        stack = [root]
        while stack:
            sid = stack.pop()
            if sid in visited:
                continue
            visited.add(sid)
            tree.order.append(sid)
            for child in reversed(children.get(sid, ())):
                if child not in visited:
                    stack.append(child)

    return tree

"""Trace tree reconstruction and structural anomaly detection.

The collector accepts spans in *any* arrival order: a parent span may be
persisted after its children. Reconstruction never relies on arrival order —
every span is indexed by id first, and parent/child edges are resolved against
that index afterwards, so late parents never lose their children.

Three structural anomalies are detected:

* **orphan span**      — ``parent_id`` names a span that does not exist in the
  same trace (cross-trace references count as missing).
* **duplicate span id** — the same ``span_id`` observed more than once in one
  trace. The first occurrence wins; later ones are reported, not silently
  merged.
* **parent cycle**      — following parent pointers loops
  (a → b → c → a). Such spans cannot belong to a tree; they are excluded from
  tree-walk based analysis (critical path, service time) but remain stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Iterator

from .model import Span

WHITE, GRAY, BLACK = 0, 1, 2


@dataclass
class TraceTree:
    trace_id: str
    spans: dict[str, Span]
    """Deduplicated spans keyed by span_id (first occurrence wins)."""
    children: dict[str, list[str]]
    """parent_id -> child span_ids, each list sorted by (start_ns, span_id)."""
    roots: list[str] = field(default_factory=list)
    orphan_span_ids: list[str] = field(default_factory=list)
    duplicate_span_ids: list[str] = field(default_factory=list)
    cycle_span_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """True when no orphan, duplicate or cycle was found."""
        return not (self.orphan_span_ids or self.duplicate_span_ids or self.cycle_span_ids)

    # ---- traversal ------------------------------------------------------------

    def preorder(self, roots: Iterable[str] | None = None) -> Iterator[str]:
        """Yield span ids reachable from ``roots`` (default: real tree roots).

        Orphans (unattached) and cycle nodes / their tails (unreachable from a
        root) are therefore excluded.
        """
        start_ids = list(self.roots) if roots is None else list(roots)
        stack: list[str] = list(reversed(start_ids))
        seen: set[str] = set()
        while stack:
            sid = stack.pop()
            if sid in seen or sid not in self.spans:
                continue
            seen.add(sid)
            yield sid
            # push children reversed so the sorted order pops out first
            for child in reversed(self.children.get(sid, ())):
                if child not in seen:
                    stack.append(child)

    def reachable_ids(self) -> set[str]:
        return set(self.preorder())

    def unreachable_ids(self) -> set[str]:
        """Cycle members plus tails that lead into cycles."""
        return set(self.spans) - self.reachable_ids()


def build_tree(spans: Iterable[Span], trace_id: str | None = None) -> TraceTree:
    """Rebuild one trace tree from an arbitrary-order span iterable.

    Spans must all belong to the same trace; if ``trace_id`` is given and a
    span with another trace id is encountered a ``ValueError`` is raised.
    """
    spans_by_id: dict[str, Span] = {}
    dup_counts: dict[str, int] = {}
    inferred_trace_id = trace_id
    ordered_input: list[Span] = []  # preserves arrival order for deterministic output

    for span in spans:
        if inferred_trace_id is None:
            inferred_trace_id = span.trace_id
        elif span.trace_id != inferred_trace_id:
            raise ValueError(
                f"build_tree received spans from multiple traces: "
                f"{inferred_trace_id!r} and {span.trace_id!r}"
            )
        ordered_input.append(span)
        if span.span_id in spans_by_id:
            dup_counts[span.span_id] = dup_counts.get(span.span_id, 1) + 1
        else:
            spans_by_id[span.span_id] = span

    if inferred_trace_id is None:
        raise ValueError("cannot build a tree from an empty span set")

    children: dict[str, list[str]] = {}
    roots: list[str] = []
    orphans: list[str] = []

    for sid, span in spans_by_id.items():
        pid = span.parent_id
        if pid is None:
            roots.append(sid)
        elif pid in spans_by_id:
            children.setdefault(pid, []).append(sid)
        else:
            orphans.append(sid)

    for child_list in children.values():
        child_list.sort(key=lambda x: (spans_by_id[x].start_ns, x))
    roots.sort(key=lambda x: (spans_by_id[x].start_ns, x))
    orphans.sort(key=lambda x: (spans_by_id[x].start_ns, x))

    cycle_members = _detect_cycles(spans_by_id)

    warnings: list[str] = []
    for sid in sorted(dup_counts):
        warnings.append(
            f"duplicate span id: {sid} ({dup_counts[sid]} observations, kept the first)"
        )
    for sid in orphans:
        warnings.append(
            f"orphan span {sid}: parent {spans_by_id[sid].parent_id} not found in trace"
        )
    for ordered_cycle in _ordered_cycles(spans_by_id, cycle_members):
        warnings.append(
            "cycle in parent chain: " + " -> ".join(ordered_cycle + [ordered_cycle[0]])
        )

    return TraceTree(
        trace_id=inferred_trace_id,
        spans=spans_by_id,
        children=children,
        roots=roots,
        orphan_span_ids=orphans,
        duplicate_span_ids=sorted(dup_counts),
        cycle_span_ids=sorted(cycle_members),
        warnings=warnings,
    )


def _detect_cycles(spans_by_id: dict[str, Span]) -> set[str]:
    """Find every span that lies on a parent cycle via iterative 3-color DFS.

    Each node has at most one outgoing edge (its parent), so each weakly
    connected component contains at most one cycle.
    """
    color: dict[str, int] = {}
    cycle_members: set[str] = set()

    for start in spans_by_id:
        if color.get(start, WHITE) != WHITE:
            continue
        path: list[str] = []
        position: dict[str, int] = {}
        cur: str | None = start
        while cur is not None:
            state = color.get(cur, WHITE)
            if state == GRAY:
                # back edge: every node from the first occurrence of cur is in it
                idx = position[cur]
                cycle_members.update(path[idx:])
                break
            if state == BLACK:
                break
            color[cur] = GRAY
            position[cur] = len(path)
            path.append(cur)
            cur = spans_by_id[cur].parent_id if cur in spans_by_id else None
            if cur is not None and cur not in spans_by_id:
                cur = None  # walked off to an orphan/missing parent
        for node in path:
            color[node] = BLACK
    return cycle_members


def _ordered_cycles(
    spans_by_id: dict[str, Span], cycle_members: set[str]
) -> list[list[str]]:
    """Return each cycle as an ordered id list (child -> parent), cycles sorted."""
    seen: set[str] = set()
    cycles: list[list[str]] = []
    for start in sorted(cycle_members):
        if start in seen:
            continue
        ordered: list[str] = []
        cur = start
        while cur not in seen:
            seen.add(cur)
            ordered.append(cur)
            cur = spans_by_id[cur].parent_id  # type: ignore[assignment]
        cycles.append(ordered)
    # deterministic order: rotate each cycle to start with its smallest id,
    # then sort cycles by that representation
    normalized = []
    for cyc in cycles:
        pivot = min(range(len(cyc)), key=lambda i: cyc[i])
        normalized.append(cyc[pivot:] + cyc[:pivot])
    normalized.sort()
    return normalized

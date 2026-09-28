"""Stable JSON and human-readable text reports."""

from __future__ import annotations

import json
from typing import Sequence


def to_json(trace: dict, indent: int = 2) -> str:
    """Stable JSON for one trace (keys sorted, fixed separators)."""
    return json.dumps(trace, ensure_ascii=False, sort_keys=True, indent=indent)


def traces_to_json(traces: Sequence[dict], indent: int = 2) -> str:
    return json.dumps(list(traces), ensure_ascii=False, sort_keys=True,
                      indent=indent)


def _fmt_us(us: int) -> str:
    if us >= 1_000_000:
        return f"{us / 1_000_000:.3f}s"
    if us >= 1_000:
        return f"{us / 1_000:.3f}ms"
    return f"{us}us"


def _tree_lines(node: dict, lines: list[str], depth: int) -> None:
    sp = node["span"]
    indent = "  " * depth
    mark = "ERROR " if sp.get("status") == "ERROR" else ""
    lines.append(
        f"{indent}- [{mark}{sp.get('status', 'OK')}] "
        f"{sp['service']}/{sp['name'] or 'span'} "
        f"span_id={sp['span_id']} "
        f"[{sp['start_us']}..{sp['end_us']}] "
        f"dur={_fmt_us(sp['end_us'] - sp['start_us'])} "
        f"self={_fmt_us(node.get('self_us', 0))}"
    )
    for ch in node.get("children", []):
        _tree_lines(ch, lines, depth + 1)


def to_text(trace: dict) -> str:
    """Deterministic text report for one trace."""
    lines: list[str] = []
    lines.append(f"Trace {trace['trace_id']}")
    lines.append(
        f"  status={trace['status']}  spans={trace['span_count']}  "
        f"errors={trace['error_count']}  "
        f"window=[{trace['start_us']}..{trace['end_us']}]  "
        f"latency={_fmt_us(trace['duration_us'])}"
    )

    lines.append("  service time (nested intervals merged):")
    for svc, t in trace.get("service_time_us", {}).items():
        lines.append(f"    {svc}: {_fmt_us(t)} ({t}us)")

    crit = trace.get("critical_path", [])
    crit_us = trace.get("critical_path_us")
    if crit_us is None:
        # Older index format: derive self time from tree nodes.
        crit_us = 0
    crit_span_us = trace.get(
        "critical_path_span_us",
        sum(s["end_us"] - s["start_us"] for s in crit),
    )
    lines.append(
        f"  critical path (exclusive/self time {_fmt_us(crit_us)}, "
        f"wall span {_fmt_us(crit_span_us)}):"
    )
    if not crit:
        lines.append("    (no spans)")
    self_by_id: dict[str, int] = {}

    def index_self(node: dict) -> None:
        self_by_id[node["span"]["span_id"]] = node.get("self_us", 0)
        for ch in node.get("children", []):
            index_self(ch)

    for root in trace.get("roots", []):
        index_self(root)
    for sp in crit:
        lines.append(
            f"    -> {sp['service']}/{sp['name'] or 'span'} "
            f"span_id={sp['span_id']} dur="
            f"{_fmt_us(sp['end_us'] - sp['start_us'])} "
            f"self={_fmt_us(self_by_id.get(sp['span_id'], 0))}"
        )

    anoms = trace.get("anomalies", [])
    lines.append(f"  anomalies: {len(anoms)}")
    for a in anoms:
        lines.append(f"    [{a['kind']}] span={a.get('span_id')}: "
                     f"{a['message']}")

    lines.append("  tree:")
    for root in trace.get("roots", []):
        _tree_lines(root, lines, 2)
    return "\n".join(lines)


def summary_to_text(summary: dict) -> str:
    lines = [
        "Trace summary",
        f"  traces: {summary.get('trace_count', 0)} "
        f"(ok={summary.get('ok_trace_count', 0)}, "
        f"error={summary.get('error_trace_count', 0)})",
        f"  spans: {summary.get('span_count', 0)}",
    ]
    d = summary.get("duration_us", {})
    if d:
        lines.append(
            "  duration: "
            f"min={_fmt_us(d['min']) if d.get('min') is not None else '-'} "
            f"avg={_fmt_us(d['avg']) if d.get('avg') is not None else '-'} "
            f"p50={_fmt_us(d['p50']) if d.get('p50') is not None else '-'} "
            f"p95={_fmt_us(d['p95']) if d.get('p95') is not None else '-'} "
            f"p99={_fmt_us(d['p99']) if d.get('p99') is not None else '-'} "
            f"max={_fmt_us(d['max']) if d.get('max') is not None else '-'}"
        )
    svcs = summary.get("services", {})
    if svcs:
        lines.append("  services: " + ", ".join(f"{k}({v})"
                                                for k, v in svcs.items()))
    ac = summary.get("anomaly_counts", {})
    if ac:
        lines.append("  anomalies: " + ", ".join(f"{k}={v}"
                                                 for k, v in ac.items()))
    return "\n".join(lines)

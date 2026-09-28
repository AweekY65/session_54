"""Stable JSON / text report rendering.

Reports are deterministic: fixed key order, sorted collections, no clock
reads, no environment leakage — the same store always produces the same
bytes.
"""

from __future__ import annotations

import json
from typing import Any

from .analysis import TraceAnalysis
from .model import Status, micros_to_iso
from .store import SpanStore

REPORT_VERSION = 1


def _span_node(analysis: TraceAnalysis, span_id: str) -> dict[str, Any]:
    span = analysis.tree.spans[span_id]
    return {
        "span_id": span.span_id,
        "parent_id": span.parent_id,
        "service": span.service,
        "name": span.name,
        "start_us": span.start_us,
        "end_us": span.end_us,
        "duration_us": span.duration_us,
        "exclusive_us": analysis.span_exclusive_us.get(span_id, 0),
        "on_critical_path": span_id in analysis.critical_path.span_ids,
        "status": span.status.value,
        "attributes": dict(sorted(span.attributes.items())),
        "children": [
            _span_node(analysis, c)
            for c in analysis.tree.children.get(span_id, ())
        ],
    }


def trace_report(analysis: TraceAnalysis) -> dict[str, Any]:
    return {
        "trace_id": analysis.trace_id,
        "start_us": analysis.start_us,
        "end_us": analysis.end_us,
        "duration_us": analysis.duration_us,
        "status": analysis.status.value,
        "span_count": analysis.span_count,
        "error_count": analysis.error_count,
        "services": analysis.services,
        "service_exclusive_us": {
            s: analysis.service_time_us[s] for s in analysis.services
        },
        "service_inclusive_us": {
            s: analysis.service_inclusive_us[s] for s in analysis.services
        },
        "critical_path": analysis.critical_path.to_dict(),
        "anomalies": [a.to_dict() for a in analysis.anomalies],
        "roots": [_span_node(analysis, r) for r in analysis.tree.roots],
    }


def _summary(analysis: TraceAnalysis) -> dict[str, Any]:
    cp = analysis.critical_path
    return {
        "trace_id": analysis.trace_id,
        "start_us": analysis.start_us,
        "end_us": analysis.end_us,
        "duration_us": analysis.duration_us,
        "status": analysis.status.value,
        "span_count": analysis.span_count,
        "error_count": analysis.error_count,
        "services": analysis.services,
        "anomaly_kinds": sorted({a.kind for a in analysis.anomalies}),
        "critical_path_span_ids": cp.span_ids,
        "critical_path_us": cp.total_us,
        "parallel_time_us": cp.parallel_time_us,
    }


def build_report(store: SpanStore, analyses: list[TraceAnalysis],
                 *, detailed: bool = False,
                 generated_filter: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assemble the full report object (stable key order)."""
    detailed_traces = [trace_report(a) for a in analyses] if detailed else []
    summaries = [] if detailed else [_summary(a) for a in analyses]

    total_duration = sum(a.duration_us for a in analyses)
    status_counts = {Status.OK.value: 0, Status.ERROR.value: 0,
                     Status.UNSET.value: 0}
    service_totals: dict[str, int] = {}
    anomaly_traces = 0
    for a in analyses:
        status_counts[a.status.value] += 1
        for svc, val in a.service_time_us.items():
            service_totals[svc] = service_totals.get(svc, 0) + val
        if a.anomalies:
            anomaly_traces += 1

    return {
        "report_version": REPORT_VERSION,
        "time_unit": "microsecond",
        "store": {
            "total_traces": len(store.trace_ids()),
            "total_spans": len(store),
        },
        "query": {
            "matched_traces": len(analyses),
            "matched_spans": sum(a.span_count for a in analyses),
            "filter": generated_filter or {},
        },
        "statistics": {
            "status_counts": status_counts,
            "total_duration_us": total_duration,
            "min_duration_us": min((a.duration_us for a in analyses),
                                   default=0),
            "max_duration_us": max((a.duration_us for a in analyses),
                                   default=0),
            "avg_duration_us": (total_duration // len(analyses)
                                if analyses else 0),
            "service_exclusive_us": {
                k: service_totals[k] for k in sorted(service_totals)
            },
            "traces_with_anomalies": anomaly_traces,
        },
        "traces": detailed_traces,
        "trace_summaries": summaries,
    }


def report_json(report: dict[str, Any], *, indent: int = 2) -> str:
    # sort_keys=False: our dicts are already built in canonical order.
    return json.dumps(report, indent=indent, ensure_ascii=False,
                      sort_keys=False) + "\n"


# --------------------------------------------------------------------- text
def report_text(report: dict[str, Any], *, iso_times: bool = False) -> str:
    """Human-readable, fixed-width, also deterministic text report."""
    def t(us: int) -> str:
        if iso_times:
            return micros_to_iso(us)
        return f"{us}us"

    lines: list[str] = []
    store = report["store"]
    query = report["query"]
    stats = report["statistics"]
    lines.append("LOCALTRACE REPORT")
    lines.append("=" * 72)
    lines.append(f"store: {store['total_spans']} spans in "
                 f"{store['total_traces']} traces")
    lines.append(f"query: {query['matched_traces']} traces / "
                 f"{query['matched_spans']} spans matched")
    lines.append("")
    lines.append("statistics")
    lines.append("-" * 72)
    lines.append(f"  status: {stats['status_counts']}")
    lines.append(f"  duration  min={stats['min_duration_us']}us "
                 f"max={stats['max_duration_us']}us "
                 f"avg={stats['avg_duration_us']}us "
                 f"total={stats['total_duration_us']}us")
    svc = stats["service_exclusive_us"]
    if svc:
        lines.append("  service exclusive time:")
        width = max(len(k) for k in svc)
        for name in sorted(svc):
            lines.append(f"    {name.ljust(width)}  {svc[name]}us")
    lines.append(f"  traces with anomalies: {stats['traces_with_anomalies']}")
    lines.append("")

    traces = report["traces"] or report["trace_summaries"]
    lines.append(f"traces ({len(traces)})")
    lines.append("-" * 72)
    for tr in traces:
        lines.extend(_render_trace(tr, t, detailed=bool(report["traces"])))
    return "\n".join(lines) + "\n"


def _render_trace(tr: dict[str, Any], t_fmt, *, detailed: bool) -> list[str]:
    out: list[str] = []
    marker = "!" if tr.get("anomaly_kinds") or tr.get("anomalies") else " "
    out.append(f"[{marker}] trace {tr['trace_id']}  "
               f"status={tr['status']}  "
               f"start={t_fmt(tr['start_us'])}  "
               f"duration={tr['duration_us']}us  "
               f"spans={tr['span_count']}  errors={tr['error_count']}")
    svc = tr.get("service_exclusive_us")
    if svc:
        out.append("      services(exclusive): "
                   + ", ".join(f"{k}={v}us" for k, v in
                               sorted(svc.items())))
    cp = tr.get("critical_path")
    if cp:
        out.append(f"      critical path ({cp['total_us']}us, "
                   f"parallel={cp.get('parallel_time_us', 0)}us): "
                   + " -> ".join(cp["span_ids"]))
    anomalies = tr.get("anomalies")
    if anomalies:
        for a in anomalies:
            out.append(f"      ANOMALY {a['kind']}: {a['span_ids']} "
                       f"{a.get('detail', '')}".rstrip())
    elif tr.get("anomaly_kinds"):
        out.append("      anomalies: " + ", ".join(tr["anomaly_kinds"]))
    if detailed:
        for root in tr["roots"]:
            out.extend(_render_span_node(root, depth=1, t_fmt=t_fmt))
    out.append("")
    return out


def _render_span_node(node: dict[str, Any], *, depth: int, t_fmt) -> list[str]:
    indent = "  " * (depth + 2)
    star = "*" if node["on_critical_path"] else " "
    text = (f"{indent}{star} {node['span_id']} "
            f"{node['service']}/{node['name'] or '-'} "
            f"[{node['status']}] start={t_fmt(node['start_us'])} "
            f"dur={node['duration_us']}us self={node['exclusive_us']}us")
    out = [text]
    if node["attributes"]:
        for key in sorted(node["attributes"]):
            out.append(f"{indent}    . {key}={node['attributes'][key]!r}")
    for child in node["children"]:
        out.extend(_render_span_node(child, depth=depth + 1, t_fmt=t_fmt))
    return out

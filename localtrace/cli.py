"""Command-line interface: ingest / import / query / report / retention.

Examples
--------
    python -m localtrace serve --data-dir ./tracedata --port 8765
    python -m localtrace import spans.jsonl --data-dir ./tracedata
    python -m localtrace simulate --receive --data-dir ./tracedata
    python -m localtrace query --service auth --status ERROR --attr env=dev
    python -m localtrace report --trace-id t-demo --format text
    python -m localtrace retain --before 2026-09-27T00:00:00Z
    python -m localtrace reindex
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .collector import Collector
from .model import to_micros
from .query import TraceFilter, query_traces
from .report import build_report, report_json, report_text
from .simulator import simulate_trace
from .store import SpanStore

DEFAULT_DATA_DIR = ".localtrace"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="localtrace",
        description="Local distributed-trace collector & analyzer "
                    "(files/memory only; no external services).",
    )
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                        help=f"local storage directory (default: {DEFAULT_DATA_DIR})")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the local HTTP receiver")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)

    p_ingest = sub.add_parser("ingest", help="read span JSON/JSONL from stdin")
    p_ingest.add_argument("--format", choices=("json", "jsonl"), default="json")

    p_import = sub.add_parser("import", help="import a span JSON/JSONL file")
    p_import.add_argument("file")

    p_sim = sub.add_parser("simulate", help="generate a simulated trace")
    p_sim.add_argument("--trace-id", default="t-demo")
    p_sim.add_argument("--seed", type=int, default=7)
    p_sim.add_argument("--receive", action="store_true",
                       help="store into data-dir instead of printing")

    def add_query_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--service", action="append", default=[],
                       help="filter by service (repeatable, AND)")
        p.add_argument("--status", action="append", default=[],
                       choices=["OK", "ERROR", "UNSET"])
        p.add_argument("--attr", action="append", default=[],
                       metavar="KEY[=VALUE]",
                       help="attribute filter; 'key'=exists, 'k=v'=equals")
        p.add_argument("--start", help="window start (micros or ISO-8601)")
        p.add_argument("--end", help="window end (micros or ISO-8601)")
        p.add_argument("--trace-id", action="append", default=None)
        p.add_argument("--limit", type=int, default=None)

    p_query = sub.add_parser("query", help="filter traces, print summaries")
    add_query_args(p_query)

    p_report = sub.add_parser("report", help="emit full JSON/text report")
    add_query_args(p_report)
    p_report.add_argument("--format", choices=("json", "text"), default="json")
    p_report.add_argument("--detailed", action="store_true",
                          help="include full span trees")
    p_report.add_argument("--output", "-o", help="write report to a file")

    p_retain = sub.add_parser("retain", help="delete old traces (retention)")
    p_retain.add_argument("--before", help="delete traces ending after this")
    p_retain.add_argument("--after", help="delete traces starting before this")
    p_retain.add_argument("--max-traces", type=int, default=None,
                          help="keep only the N most recently started traces")

    sub.add_parser("reindex", help="rebuild the local index from spans.jsonl")
    sub.add_parser("stats", help="print store statistics as JSON")

    return parser


def _filter_from_args(args: argparse.Namespace) -> TraceFilter:
    attrs, keys_only = TraceFilter.parse_attributes(getattr(args, "attr", []))
    return TraceFilter(
        services=list(args.service),
        statuses=list(args.status),
        attributes=attrs,
        attribute_keys_only=keys_only,
        start_us=to_micros(args.start) if args.start else None,
        end_us=to_micros(args.end) if args.end else None,
        trace_ids=list(args.trace_id) if args.trace_id else None,
    )


def _cmd_ingest(store: SpanStore, args: argparse.Namespace) -> int:
    raw = sys.stdin.read()
    if args.format == "jsonl":
        items = [json.loads(line) for line in raw.splitlines() if line.strip()]
    else:
        data = json.loads(raw)
        items = data if isinstance(data, list) else [data]
    result = Collector(store).receive(items)
    print(json.dumps(result.to_dict(), sort_keys=True))
    return 0


def _cmd_import(store: SpanStore, args: argparse.Namespace) -> int:
    result = store.import_file(args.file)
    print(json.dumps(result.to_dict(), sort_keys=True))
    return 0


def _cmd_simulate(store: SpanStore, args: argparse.Namespace) -> int:
    spans = simulate_trace(args.trace_id, seed=args.seed)
    if args.receive:
        result = Collector(store).receive(spans)
        print(json.dumps(result.to_dict(), sort_keys=True))
    else:
        print(json.dumps([s.to_dict(iso_times=True) for s in spans], indent=2))
    return 0


def _cmd_query(store: SpanStore, args: argparse.Namespace) -> int:
    flt = _filter_from_args(args)
    analyses = query_traces(store, flt, limit=args.limit)
    rows = [
        {
            "trace_id": a.trace_id,
            "duration_us": a.duration_us,
            "status": a.status.value,
            "services": a.services,
            "span_count": a.span_count,
            "anomalies": [x.kind for x in a.anomalies],
            "critical_path": a.critical_path.span_ids,
        }
        for a in analyses
    ]
    print(json.dumps(rows, indent=2))
    return 0


def _cmd_report(store: SpanStore, args: argparse.Namespace) -> int:
    flt = _filter_from_args(args)
    analyses = query_traces(store, flt, limit=args.limit)
    report = build_report(store, analyses, detailed=args.detailed,
                          generated_filter=flt.to_dict())
    text = (report_json(report) if args.format == "json"
            else report_text(report))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


def _cmd_retain(store: SpanStore, args: argparse.Namespace) -> int:
    before = to_micros(args.before) if args.before else None
    after = to_micros(args.after) if args.after else None
    summary = store.retain(before_us=before, after_us=after,
                           max_traces=args.max_traces)
    print(json.dumps(summary, sort_keys=True))
    return 0


def _cmd_reindex(store: SpanStore, args: argparse.Namespace) -> int:
    print(json.dumps(store.reindex(), sort_keys=True))
    return 0


def _cmd_stats(store: SpanStore, args: argparse.Namespace) -> int:
    payload = {
        "data_dir": store.data_dir,
        "span_count": len(store),
        "trace_count": len(store.trace_ids()),
        "trace_status_counts": store.status_counts(),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _cmd_serve(store: SpanStore, args: argparse.Namespace) -> int:
    from .server import start_server

    handle = start_server(store, host=args.host, port=args.port)
    host, port = handle.server.server_address[:2]
    sys.stderr.write(f"localtrace receiver listening on http://{host}:{port}\n")
    sys.stderr.write("POST spans to /spans; Ctrl-C to stop.\n")
    try:
        handle.thread.join()
    except KeyboardInterrupt:
        handle.stop()
    return 0


_COMMANDS = {
    "ingest": _cmd_ingest,
    "import": _cmd_import,
    "simulate": _cmd_simulate,
    "query": _cmd_query,
    "report": _cmd_report,
    "retain": _cmd_retain,
    "reindex": _cmd_reindex,
    "stats": _cmd_stats,
    "serve": _cmd_serve,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    store = SpanStore(args.data_dir)
    try:
        return _COMMANDS[args.command](store, args)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

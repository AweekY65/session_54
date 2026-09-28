"""Command-line interface (all data stays local).

Examples:
    python -m tracelet demo --count 3 --reindex
    python -m tracelet import spans.jsonl
    python -m tracelet list --service payment --status ERROR
    python -m tracelet get tr-demo --format json
    python -m tracelet summary
    python -m tracelet reindex
    python -m tracelet retention --keep-last 5
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from . import __version__, report
from .collector import Collector
from .query import make_filter
from .simulator import simulate_traces, spans_out_of_order
from .storage import LocalStore


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tracelet",
                                description="Local distributed trace tool")
    p.add_argument("--data-dir", default=".tracelet-data",
                   help="local storage directory (default: ./.tracelet-data)")
    p.add_argument("--memory", action="store_true",
                   help="keep everything in process memory (no files)")
    p.add_argument("--version", action="version",
                   version=f"tracelet {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("demo", help="generate spans from local simulated "
                                     "services and ingest them")
    sp.add_argument("--count", type=int, default=3)
    sp.add_argument("--seed", type=int, default=7)
    sp.add_argument("--reindex", action="store_true", default=True)

    si = sub.add_parser("import", help="import spans from a local file")
    si.add_argument("path")
    si.add_argument("--format", choices=("jsonl", "json"), default="jsonl")

    sl = sub.add_parser("list", help="list traces (filters are AND-ed)")
    sl.add_argument("--service")
    sl.add_argument("--status", choices=("OK", "ERROR"))
    sl.add_argument("--attr", help="attribute key or key=value")
    sl.add_argument("--start", help="start of time window (us or ISO-8601)")
    sl.add_argument("--end", help="end of time window (us or ISO-8601)")
    sl.add_argument("--limit", type=int)
    sl.add_argument("--format", choices=("text", "json"), default="text")

    sg = sub.add_parser("get", help="show one trace")
    sg.add_argument("trace_id")
    sg.add_argument("--format", choices=("text", "json"), default="text")

    sub.add_parser("summary", help="cross-trace statistics")
    sub.add_parser("reindex", help="rebuild all indexes from the span log")

    sr = sub.add_parser("retention", help="delete old traces locally")
    sr.add_argument("--keep-last", type=int,
                    help="keep only the N most recent traces")
    sr.add_argument("--max-age-us", type=int,
                    help="delete traces older than this many microseconds")

    sub.add_parser("ooo", help="ingest one out-of-order trace and print it "
                               "(demonstrates late-parent handling)")
    return p


def _collector(args: argparse.Namespace) -> Collector:
    if getattr(args, "memory", False):
        return Collector(LocalStore(None))
    return Collector(LocalStore(args.data_dir))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    c = _collector(args)

    if args.cmd == "demo":
        spans = simulate_traces(args.count, seed=args.seed)
        r = c.ingest_many(spans)
        c.reindex()
        print(f"ingested accepted={r.accepted} duplicates={r.duplicates} "
              f"rejected={r.rejected}")
        for e in r.errors:
            print(f"  ! {e}", file=sys.stderr)
        print(report.summary_to_text(c.summary()))

    elif args.cmd == "import":
        if args.format == "jsonl":
            r = c.import_jsonl(Path(args.path))
        else:
            r = c.import_json_array(Path(args.path))
        c.reindex()
        print(f"imported accepted={r.accepted} duplicates={r.duplicates} "
              f"rejected={r.rejected}")
        for e in r.errors:
            print(f"  ! {e}", file=sys.stderr)

    elif args.cmd == "list":
        flt = make_filter(service=args.service, status=args.status,
                          attr=args.attr, start=args.start, end=args.end)
        traces = c.query(flt, limit=args.limit)
        if args.format == "json":
            print(report.traces_to_json(traces))
        else:
            if not traces:
                print("(no traces match)")
            for td in traces:
                print(f"{td['trace_id']}  status={td['status']}  "
                      f"spans={td['span_count']}  "
                      f"latency={td['duration_us']}us  "
                      f"anomalies={len(td['anomalies'])}")

    elif args.cmd == "get":
        td = c.get(args.trace_id)
        if td is None:
            print(f"trace {args.trace_id!r} not found", file=sys.stderr)
            return 1
        print(report.to_json(td) if args.format == "json"
              else report.to_text(td))

    elif args.cmd == "summary":
        c.reindex()
        print(report.summary_to_text(c.summary()))

    elif args.cmd == "reindex":
        traces = c.reindex()
        print(f"rebuilt {len(traces)} trace indexes in "
              f"{c.store.path or '<memory>'}")

    elif args.cmd == "retention":
        if args.keep_last is None and args.max_age_us is None:
            print("nothing to do: pass --keep-last and/or --max-age-us",
                  file=sys.stderr)
            return 2
        info = c.retention(keep_last=args.keep_last,
                           max_age_us=args.max_age_us)
        print(f"retention deleted_traces={info['deleted_trace_count']} "
              f"deleted_spans={info['deleted_span_count']} "
              f"kept_traces={info['kept_trace_count']}")

    elif args.cmd == "ooo":
        # In-memory: deliberately feed children before the root.
        c = Collector(LocalStore(None))
        c.ingest_many(spans_out_of_order("tr-ooo"))
        c.reindex()
        td = c.get("tr-ooo")
        print(report.to_text(td))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

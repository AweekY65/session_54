"""localtrace: a fully local, file/in-memory distributed-trace collector & analyzer.

No Jaeger / Zipkin / OTel Collector / database / cloud dependency.
All spans, indexes and reports live in local files or in memory.
"""

from .model import Span, Status, to_micros
from .store import SpanStore
from .collector import Collector
from .tree import BuiltTree, build_trace_tree
from .analysis import TraceAnalysis, analyze_trace
from .query import TraceFilter, query_traces
from .report import build_report, report_json, report_text
from .server import ServerHandle, post_spans, start_server

__version__ = "1.0.0"

__all__ = [
    "Span",
    "Status",
    "to_micros",
    "SpanStore",
    "Collector",
    "BuiltTree",
    "build_trace_tree",
    "TraceAnalysis",
    "analyze_trace",
    "TraceFilter",
    "query_traces",
    "build_report",
    "report_json",
    "report_text",
    "ServerHandle",
    "post_spans",
    "start_server",
    "__version__",
]

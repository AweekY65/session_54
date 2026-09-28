"""localtrace: a fully local, dependency-free distributed trace collector.

No Jaeger / Zipkin / OTel Collector / database / cloud service is involved:
spans, traces, indexes and reports live only in local files or in memory.
"""

from .model import Span, VALID_STATUSES
from .store import LocalTraceStore
from .tree import TraceTree, build_tree
from .analysis import TraceAnalysis, analyze
from .query import TraceFilter, query_traces
from .report import build_report, report_to_json, report_to_text
from .retention import apply_retention, parse_cutoff

__all__ = [
    "Span",
    "VALID_STATUSES",
    "LocalTraceStore",
    "TraceTree",
    "build_tree",
    "TraceAnalysis",
    "analyze",
    "TraceFilter",
    "query_traces",
    "build_report",
    "report_to_json",
    "report_to_text",
    "apply_retention",
    "parse_cutoff",
]

__version__ = "1.0.0"

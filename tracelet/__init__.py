"""tracelet: a fully local, file-and-memory based distributed trace tool.

No Jaeger / Zipkin / OTel Collector / database / cloud service is involved.
Every span, trace, index, statistic and report lives in local files or in
process memory. See README.md for the data model and file formats.
"""

from .model import Span, ModelError, parse_time_us
from .analyzer import (
    Anomaly,
    Trace,
    TraceNode,
    build_trace,
    build_traces,
    merge_intervals,
)
from .collector import Collector, IngestResult
from .storage import LocalStore
from .query import Filter, list_traces
from . import report

__version__ = "1.0.0"

__all__ = [
    "Span",
    "ModelError",
    "parse_time_us",
    "Anomaly",
    "Trace",
    "TraceNode",
    "build_trace",
    "build_traces",
    "merge_intervals",
    "Collector",
    "IngestResult",
    "LocalStore",
    "Filter",
    "list_traces",
    "report",
    "__version__",
]

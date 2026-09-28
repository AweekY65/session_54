"""Local file / in-memory storage.

File-backed layout (created on first write)::

    <data_dir>/
      spans.jsonl          # accepted spans, one JSON object per line
      events.jsonl        # anomaly events detected during analysis
      index/
        tr_<trace>.json   # one fully analyzed trace per file
        summary.json      # cross-trace statistics

Filenames use a short ascii-safe encoding of the trace id
(``tr_`` + hex when it contains non-word characters) so any client-supplied
id is safe on disk.

Pass ``data_dir=None`` for pure in-memory storage: nothing ever touches disk.
Writes go through a temp-file + atomic replace, so a killed process can never
leave a half-written index or log.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Iterable, Optional

from .model import Span

_SAFE_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _trace_filename(trace_id: str) -> str:
    if _SAFE_RE.match(trace_id):
        return f"tr_{trace_id}.json"
    return "tr_" + trace_id.encode("utf-8").hex() + ".json"


class LocalStore:
    """File-backed when ``data_dir`` is given, purely in-memory otherwise."""

    def __init__(self, data_dir: Optional[str | os.PathLike] = None):
        if data_dir is None:
            self.path: Optional[Path] = None
            self._mem_spans: list[Span] = []
            self._mem_events: list[dict] = []
            self._mem_index: dict[str, dict] = {}
            self._mem_summary: Optional[dict] = None
        else:
            self.path = Path(data_dir)
            (self.path / "index").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ logs
    @property
    def spans_path(self) -> Optional[Path]:
        return self.path / "spans.jsonl" if self.path else None

    @property
    def events_path(self) -> Optional[Path]:
        return self.path / "events.jsonl" if self.path else None

    def load_spans(self) -> list[Span]:
        if self.path is None:
            return list(self._mem_spans)
        if not self.spans_path.exists():
            return []
        spans: list[Span] = []
        with self.spans_path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    spans.append(Span.from_dict(json.loads(line)))
                except (json.JSONDecodeError, ValueError) as exc:
                    raise RuntimeError(
                        f"corrupt span log {self.spans_path}:{line_no}: {exc}"
                    ) from exc
        return spans

    def append_span(self, span: Span) -> None:
        if self.path is None:
            self._mem_spans.append(span)
            return
        with self.spans_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(span.to_dict(), ensure_ascii=False,
                                sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def append_spans(self, spans: Iterable[Span]) -> None:
        for sp in spans:
            self.append_span(sp)

    def load_events(self) -> list[dict]:
        if self.path is None:
            return list(self._mem_events)
        if not self.events_path.exists():
            return []
        out: list[dict] = []
        with self.events_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def rewrite_logs(self, spans: list[Span], events: list[dict]) -> None:
        """Atomically replace both logs (used by retention cleanup)."""
        if self.path is None:
            self._mem_spans = list(spans)
            self._mem_events = list(events)
            return
        self._atomic_write_lines(
            self.spans_path,
            (json.dumps(s.to_dict(), ensure_ascii=False, sort_keys=True)
             for s in spans),
        )
        self._atomic_write_lines(
            self.events_path,
            (json.dumps(e, ensure_ascii=False, sort_keys=True)
             for e in events),
        )

    def write_events(self, events: list[dict]) -> None:
        """Atomically replace the event log (used by reindex)."""
        if self.path is None:
            self._mem_events = list(events)
            return
        self._atomic_write_lines(
            self.events_path,
            (json.dumps(e, ensure_ascii=False, sort_keys=True)
             for e in events),
        )

    def _atomic_write_lines(self, target: Path, lines: Iterable[str]) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for line in lines:
                    fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ----------------------------------------------------------------- index
    @property
    def index_dir(self) -> Optional[Path]:
        return self.path / "index" if self.path else None

    def _index_file(self, trace_id: str) -> Optional[Path]:
        return self.index_dir / _trace_filename(trace_id)

    def write_index(self, trace: dict) -> None:
        tid = trace["trace_id"]
        if self.path is None:
            self._mem_index[tid] = trace
            return
        target = self._index_file(tid)
        payload = json.dumps(trace, ensure_ascii=False, sort_keys=True,
                             indent=2)
        self._atomic_write_lines(target, [payload])

    def read_index(self, trace_id: str) -> Optional[dict]:
        if self.path is None:
            return self._mem_index.get(trace_id)
        f = self._index_file(trace_id)
        if not f.exists():
            return None
        with f.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def delete_index(self, trace_id: str) -> None:
        if self.path is None:
            self._mem_index.pop(trace_id, None)
            return
        f = self._index_file(trace_id)
        try:
            f.unlink()
        except FileNotFoundError:
            pass

    def list_index_ids(self) -> list[str]:
        if self.path is None:
            return sorted(self._mem_index)
        out: list[str] = []
        for f in self.index_dir.glob("tr_*.json"):
            try:
                with f.open("r", encoding="utf-8") as fh:
                    out.append(json.load(fh)["trace_id"])
            except (json.JSONDecodeError, KeyError):
                continue
        return sorted(out)

    def clear_indexes(self) -> None:
        if self.path is None:
            self._mem_index.clear()
            return
        for f in self.index_dir.glob("tr_*.json"):
            f.unlink()

    # --------------------------------------------------------------- summary
    @property
    def summary_path(self) -> Optional[Path]:
        return self.index_dir / "summary.json" if self.path else None

    def write_summary(self, summary: dict) -> None:
        if self.path is None:
            self._mem_summary = summary
            return
        self._atomic_write_lines(
            self.summary_path,
            [json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2)],
        )

    def read_summary(self) -> Optional[dict]:
        if self.path is None:
            return self._mem_summary
        if not self.summary_path.exists():
            return None
        with self.summary_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

"""End-to-end CLI tests run as real terminal subprocesses."""

import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cli(*args, data_dir, stdin=None):
    cmd = [sys.executable, "-m", "localtrace", "--data-dir", data_dir, *args]
    return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True,
                          input=stdin, timeout=30)


def test_cli_simulate_import_query_report_retain(tmp_path):
    data = str(tmp_path / "data")

    # 1) simulate a trace and receive it into the local store
    p = run_cli("simulate", "--receive", "--trace-id", "t1", "--seed", "1",
                data_dir=data)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["accepted"] == 7

    # second trace, newer, with an error
    p = run_cli("simulate", "--receive", "--trace-id", "t2", "--seed", "3",
                data_dir=data)
    assert p.returncode == 0, p.stderr

    # 2) stats
    p = run_cli("stats", data_dir=data)
    stats = json.loads(p.stdout)
    assert stats["trace_count"] == 2

    # 3) query by service
    p = run_cli("query", "--service", "payment", data_dir=data)
    rows = json.loads(p.stdout)
    assert {r["trace_id"] for r in rows} == {"t1", "t2"}

    # 4) query by status
    p = run_cli("query", "--status", "ERROR", data_dir=data)
    rows = json.loads(p.stdout)
    assert [r["trace_id"] for r in rows] == ["t2"]

    # 5) JSON report, deterministic
    p1 = run_cli("report", "--format", "json", data_dir=data)
    p2 = run_cli("report", "--format", "json", data_dir=data)
    assert p1.stdout == p2.stdout
    report = json.loads(p1.stdout)
    assert report["query"]["matched_traces"] == 2

    # 6) text report renders
    p = run_cli("report", "--format", "text", "--detailed", data_dir=data)
    assert p.returncode == 0
    assert "LOCALTRACE REPORT" in p.stdout

    # 7) retention: drop traces that ended at/before 1,500,000 us.
    #    t1 spans end at 1,600,000 (base 1m + 600) -> kept; seed the store
    #    with an older trace first, then drop it.
    old = (
        '{"trace_id": "t-old", "span_id": "r", "service": "gateway", '
        '"start_us": 1000, "end_us": 2000}'
    )
    p = run_cli("ingest", "--format", "jsonl", data_dir=data, stdin=old)
    assert p.returncode == 0, p.stderr
    # demo traces live around 1,000,000us; cutoff 5,000 only reaches t-old
    p = run_cli("retain", "--before", "5000", data_dir=data)
    summary = json.loads(p.stdout)
    assert summary["deleted_trace_ids"] == ["t-old"]

    # 8) post-retention query cannot return the deleted trace
    p = run_cli("query", data_dir=data)
    rows = json.loads(p.stdout)
    assert all(r["trace_id"] != "t-old" for r in rows)
    assert {r["trace_id"] for r in rows} == {"t1", "t2"}


def test_cli_ingest_stdin_jsonl(tmp_path):
    data = str(tmp_path / "data")
    spans = "\n".join([
        json.dumps({"trace_id": "x", "span_id": "a", "service": "s",
                    "start_us": 0, "end_us": 10}),
        json.dumps({"trace_id": "x", "span_id": "b", "parent_id": "a",
                    "service": "s", "start_us": 2, "end_us": 8}),
    ])
    p = run_cli("ingest", "--format", "jsonl", data_dir=data, stdin=spans)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["accepted"] == 2

    p = run_cli("query", "--attr", "trace=x", data_dir=data)
    # no such attribute -> empty; then filter by trace via report
    assert p.returncode == 0


def test_cli_reindex(tmp_path):
    data = str(tmp_path / "data")
    run_cli("simulate", "--receive", data_dir=data)
    p = run_cli("reindex", data_dir=data)
    info = json.loads(p.stdout)
    assert info["span_count"] == 7
    assert os.path.exists(os.path.join(data, "index.json"))

"""Local HTTP receiver + simulated service process (in-process and real proc)."""

import json
import subprocess
import sys
import urllib.request

import pytest

from conftest import make_span
from localtrace.collector import Collector
from localtrace.server import post_spans, start_server
from localtrace.simulator import simulate_trace
from localtrace.store import SpanStore


@pytest.fixture
def handle(tmp_path):
    store = SpanStore(str(tmp_path / "srv"))
    h = start_server(store, host="127.0.0.1", port=0)
    yield h
    h.stop()


def test_healthz(handle):
    with urllib.request.urlopen(handle.url + "/healthz", timeout=5) as resp:
        assert resp.status == 200
        assert json.loads(resp.read())["status"] == "ok"


def test_post_single_and_batch(handle):
    reply = post_spans(handle.spans_url, [make_span(trace_id="t", span_id="a")])
    assert reply["accepted"] == 1

    # envelope form {"spans": [...]}, second batch references the first
    child = make_span(trace_id="t", span_id="b", parent_id="a",
                      service="svc2", start=2, end=8)
    req = urllib.request.Request(
        handle.spans_url,
        data=json.dumps({"spans": [child.to_dict()]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 202
        assert json.loads(resp.read())["accepted"] == 1
    assert len(handle.collector.store) == 2


def test_post_invalid_span_returns_400(handle):
    req = urllib.request.Request(
        handle.spans_url, data=b"{not json",
        headers={"Content-Type": "application/json"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 400


def test_simulated_trace_ingested_out_of_order(handle):
    spans = simulate_trace("t-sim", seed=1)
    reply = post_spans(handle.spans_url, spans)
    assert reply["accepted"] == len(spans)
    analysis = handle.collector.store.analyze_trace("t-sim")
    assert analysis.tree.roots  # links recovered despite shuffled arrival
    assert analysis.critical_path.span_ids[0] == "s1"
    assert analysis.duration_us == 600
    # critical-path contributions never overlap and fit within the trace
    cp = analysis.critical_path
    assert (sum(n["contribution_us"] for n in cp.nodes)
            + cp.parallel_time_us) == analysis.duration_us


def test_simulated_trace_seed_zero_has_error(handle):
    reply = post_spans(handle.spans_url, simulate_trace("t-err", seed=3))
    assert reply["accepted"] >= 1
    analysis = handle.collector.store.analyze_trace("t-err")
    assert analysis.error_count == 1
    assert analysis.status.value == "ERROR"


def test_receive_from_real_subprocess(tmp_path):
    """A separate OS process generates spans and POSTs to the local server."""
    store = SpanStore(str(tmp_path / "proc"))
    server = start_server(store, host="127.0.0.1", port=0)
    try:
        env_spans = simulate_trace("t-proc", seed=11)
        proc = subprocess.run(
            [sys.executable, "-m", "localtrace.simulator",
             "--url", server.spans_url, "--trace-id", "t-proc",
             "--seed", "11"],
            cwd=importlib_repo_root(), capture_output=True, text=True,
            timeout=30, check=True,
        )
        reply = json.loads(proc.stdout.strip())
        assert reply["accepted"] == len(env_spans)
        assert store.trace_ids() == ["t-proc"]
    finally:
        server.stop()


def importlib_repo_root():
    import os
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

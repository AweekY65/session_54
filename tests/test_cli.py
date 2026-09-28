"""End-to-end CLI tests run in a subprocess (real terminal invocation)."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def run(args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "tracelet", *args],
        cwd=cwd, text=True, capture_output=True, check=False,
    )


class CliTests(unittest.TestCase):
    def test_full_lifecycle_in_terminal(self):
        with tempfile.TemporaryDirectory() as d:
            r = run(["--data-dir", str(Path(d) / "data"),
                     "demo", "--count", "4"], cwd=Path(__file__).parents[1])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("ingested accepted=", r.stdout)

            # list: one of the generated traces carries an ERROR
            r = run(["--data-dir", str(Path(d) / "data"),
                     "list", "--status", "ERROR", "--format", "json"],
                    cwd=Path(__file__).parents[1])
            self.assertEqual(r.returncode, 0, r.stderr)
            rows = json.loads(r.stdout)
            self.assertTrue(rows)
            self.assertTrue(all(t["status"] == "ERROR" for t in rows))

            # filter by service + attribute
            r = run(["--data-dir", str(Path(d) / "data"),
                     "list", "--service", "payment",
                     "--attr", "currency=CNY"],
                    cwd=Path(__file__).parents[1])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("tr-", r.stdout)

            # get one trace as JSON
            tid = rows[0]["trace_id"]
            r = run(["--data-dir", str(Path(d) / "data"),
                     "get", tid, "--format", "json"],
                    cwd=Path(__file__).parents[1])
            td = json.loads(r.stdout)
            self.assertEqual(td["trace_id"], tid)
            self.assertIn("critical_path", td)

            # orphan trace is the last one
            r = run(["--data-dir", str(Path(d) / "data"),
                     "list"], cwd=Path(__file__).parents[1])
            r2 = run(["--data-dir", str(Path(d) / "data"),
                      "get", "tr-003"], cwd=Path(__file__).parents[1])
            self.assertIn("orphan_span", r2.stdout)

            # retention: keep only 2
            r = run(["--data-dir", str(Path(d) / "data"),
                     "retention", "--keep-last", "2"],
                    cwd=Path(__file__).parents[1])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("deleted_traces=2", r.stdout)

            r = run(["--data-dir", str(Path(d) / "data"), "list"],
                    cwd=Path(__file__).parents[1])
            self.assertNotIn("tr-000", r.stdout)
            self.assertNotIn("tr-001", r.stdout)
            self.assertIn("tr-002", r.stdout)

            # reindex after retention keeps the same result
            r = run(["--data-dir", str(Path(d) / "data"), "reindex"],
                    cwd=Path(__file__).parents[1])
            self.assertEqual(r.returncode, 0, r.stderr)
            r = run(["--data-dir", str(Path(d) / "data"), "list"],
                    cwd=Path(__file__).parents[1])
            self.assertNotIn("tr-000", r.stdout)
            self.assertIn("tr-003", r.stdout)

    def test_in_memory_ooo_command(self):
        r = run(["--memory", "ooo"], cwd=Path(__file__).parents[1])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("critical path", r.stdout)
        self.assertIn("anomalies: 0", r.stdout)


if __name__ == "__main__":
    unittest.main()

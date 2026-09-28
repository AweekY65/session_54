#!/usr/bin/env bash
# Run every tracelet test in the terminal. No external services required.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m unittest discover -s tests -v

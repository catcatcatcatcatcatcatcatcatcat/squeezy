#!/bin/sh
# Run squeezy directly from source — no install needed.
# Usage: ./run.sh [squeezy args...]
#   ./run.sh -n MySpeaker -vv
#   ./run.sh -l
cd "$(dirname "$0")"
PYTHONPATH=src exec python3 -m squeezy "$@"

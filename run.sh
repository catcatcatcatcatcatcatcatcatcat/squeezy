#!/bin/sh
# Run squeezy directly from source — no install needed.
# Usage: ./run.sh [squeezy args...]
#   ./run.sh -n MySpeaker -vv
#   ./run.sh -l
#
# Sync debugging:
#   ./run.sh -n MySpeaker -vv 2>&1 | tee /tmp/sq.log
#   grep SYNC /tmp/sq.log   # clean sync timeline
cd "$(dirname "$0")"
PYTHONPATH=src exec python3 -m squeezy "$@"

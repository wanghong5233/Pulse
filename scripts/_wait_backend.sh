#!/usr/bin/env bash
set -eu
URL="${1:-http://127.0.0.1:8010/api/runtime/patrols/job_greet.patrol}"
for i in 1 2 3 4 5 6 7 8; do
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$URL" 2>/dev/null || echo 000)
    echo "try=$i code=$code"
    if [ "$code" = "200" ]; then exit 0; fi
    sleep 5
done
exit 1

#!/usr/bin/env bash
# Wrapper for every scheduled b24-ai-auditor job.
#
#   * flock       — a slow run must not overlap the next tick. chat_poller fires
#                   every minute and shares data/chat_last_id.txt, so two live
#                   processes duplicate chat replies and lose the cursor.
#   * alerting    — a non-zero exit notifies ADMIN_USER_ID. Without this a failed
#                   audit is silent: the report simply never arrives.
#
# Usage: scripts/cron_job.sh <job-name> <command...>
set -uo pipefail

if [ "$#" -lt 2 ]; then
    echo "Usage: $0 <job-name> <command...>" >&2
    exit 2
fi

JOB="$1"
shift

LOCK_FILE="/tmp/b24-auditor-${JOB}.lock"
exec 9>"$LOCK_FILE" || exit 1
if ! flock -n 9; then
    echo "$(date -Is) [$JOB] previous run still active — skipping this tick"
    exit 0
fi

echo "$(date -Is) [$JOB] start"
OUTPUT="$("$@" 2>&1)"
CODE=$?
printf '%s\n' "$OUTPUT"
echo "$(date -Is) [$JOB] finished with code $CODE"

if [ "$CODE" -ne 0 ]; then
    TAIL="$(printf '%s' "$OUTPUT" | tail -c 1200)"
    docker run --rm --env-file .env \
        -v "$(pwd)/logs:/app/logs" -v "$(pwd)/data:/app/data" \
        b24-ai-auditor:latest python src/job_alert.py "$JOB" "$CODE" "$TAIL" \
        || echo "$(date -Is) [$JOB] alert delivery failed" >&2
fi

exit "$CODE"

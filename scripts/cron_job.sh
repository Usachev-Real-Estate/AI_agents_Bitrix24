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

# Предохранитель на общий лог. Штатно его ротирует logrotate
# (deploy/logrotate-b24), но если конфиг не установлен — а на боевом сервере
# он не был установлен, и файл дорос до 14 ГБ при диске в 41, — то кончится
# место, и встанет всё сразу: ETL, аудит, дашборд. Порог здесь заведомо выше
# рабочего размера, чтобы срабатывать только там, где штатной ротации нет.
#
# truncate, а не rm: файл открыт на дозапись работающими задачами, и удаление
# оставило бы место занятым до их завершения. Хвост сохраняется — лог нужен
# ровно тогда, когда что-то сломалось, то есть в последних записях.
CRON_LOG="logs/cron.log"
CRON_LOG_LIMIT=$((1024 * 1024 * 1024))
CRON_LOG_KEEP=$((50 * 1024 * 1024))
if [ -f "$CRON_LOG" ] \
   && [ "$(stat -c%s "$CRON_LOG" 2>/dev/null || echo 0)" -gt "$CRON_LOG_LIMIT" ]; then
    tail -c "$CRON_LOG_KEEP" "$CRON_LOG" > "${CRON_LOG}.keep" 2>/dev/null \
        && truncate -s 0 "$CRON_LOG" \
        && cat "${CRON_LOG}.keep" >> "$CRON_LOG"
    rm -f "${CRON_LOG}.keep"
    echo "$(date -Is) [$JOB] cron.log обрезан: не установлен logrotate," \
         "см. deploy/logrotate-b24"
fi

echo "$(date -Is) [$JOB] start"

# Вывод идёт в лог по мере появления и одновременно копится в файл для алерта.
# Захват в переменную ("$(...)") задерживал бы всё до конца задачи: у аудита
# это 25 минут тишины в cron.log, неотличимой от зависшего процесса.
OUT_FILE="$(mktemp -t "b24-${JOB}-XXXXXX.log")"
trap 'rm -f "$OUT_FILE"' EXIT

"$@" 2>&1 | tee "$OUT_FILE"
CODE="${PIPESTATUS[0]}"

echo "$(date -Is) [$JOB] finished with code $CODE"

if [ "$CODE" -ne 0 ]; then
    TAIL="$(tail -c 1200 "$OUT_FILE")"
    docker run --rm --env-file .env \
        -v "$(pwd)/logs:/app/logs" -v "$(pwd)/data:/app/data" \
        b24-ai-auditor:latest python src/job_alert.py "$JOB" "$CODE" "$TAIL" \
        || echo "$(date -Is) [$JOB] alert delivery failed" >&2
fi

exit "$CODE"

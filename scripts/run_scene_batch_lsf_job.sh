#!/usr/bin/env bash
set -euo pipefail

# Run one deterministic scene-batch chunk inside one LSF job.
#
# Usage:
#   bash scripts/run_scene_batch_lsf_job.sh OUTPUT_ROOT PART START_SEED COUNT \
#       [PARALLEL_WORKERS] [BASE_DISPLAY_NUMBER] [MAX_ATTEMPTS] \
#       [INCLUDE_DESKTOP_CHROME] [APP_ALLOWLIST] [SESSION_GROUP_SIZE]

# Accept 1/yes/on as well as true. Passing INCLUDE_DESKTOP_CHROME=1 used to be
# read as false, which silently added --no-include-chrome while the log still
# printed the raw value, so submitted jobs lost desktop chrome annotations.
normalize_bool() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) printf 'true' ;;
        *) printf 'false' ;;
    esac
}

OUTPUT_ROOT="${1:?output root required}"
PART="${2:?part required}"
START_SEED="${3:?start seed required}"
COUNT="${4:?count required}"
PARALLEL_WORKERS="${5:-1}"
BASE_DISPLAY_NUMBER="${6:-$((400 + PART * 32))}"
MAX_ATTEMPTS="${7:-$((COUNT * 20))}"
INCLUDE_DESKTOP_CHROME="$(normalize_bool "${8:-true}")"
APP_ALLOWLIST="${9:-}"
SESSION_GROUP_SIZE="${10:-${SESSION_GROUP_SIZE:-1}}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUTPUT_ROOT="$(python - <<'PY' "${OUTPUT_ROOT}"
import os, sys
print(os.path.abspath(sys.argv[1]))
PY
)"
PART_PADDED="$(printf '%04d' "${PART}")"
JOB_DIR="${OUTPUT_ROOT}/jobs/part_${PART_PADDED}"
LSF_DIR="${OUTPUT_ROOT}/.lsbatch"
DONE_DIR="${LSF_DIR}/parts_done"
FAILED_DIR="${LSF_DIR}/parts_failed"
MERGE_LOCK="${LSF_DIR}/merge.lock"
RUN_LOG="${JOB_DIR}/job_runner.log"
DONE_MARKER="${DONE_DIR}/part_${PART_PADDED}.done"
FAILED_MARKER="${FAILED_DIR}/part_${PART_PADDED}.failed"
MERGE_EACH_JOB="$(normalize_bool "${MERGE_EACH_JOB:-false}")"
RETRY_MARKER="${FAILED_DIR}/part_${PART_PADDED}.transient"
MAX_TRANSIENT_RETRIES="${MAX_TRANSIENT_RETRIES:-3}"

mkdir -p "${JOB_DIR}" "${DONE_DIR}" "${FAILED_DIR}"
export TMPDIR="${TMPDIR:-${JOB_DIR}/tmp}"
export DESKSHOT_BIN_FIX_DIR="${DESKSHOT_BIN_FIX_DIR:-${TMPDIR}/deskshot_bin_fix}"
mkdir -p "${TMPDIR}" "${DESKSHOT_BIN_FIX_DIR}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "${RUN_LOG}"
}

CHILD_PID=""

terminate_child() {
    if [[ -n "${CHILD_PID}" ]] && kill -0 "${CHILD_PID}" 2>/dev/null; then
        # SIGINT lets Python raise KeyboardInterrupt and unwind DesktopSession,
        # which cleans Xvfb/D-Bus/AT-SPI children. Escalate only if it hangs.
        kill -INT "${CHILD_PID}" 2>/dev/null || true
        for _ in {1..10}; do
            if ! kill -0 "${CHILD_PID}" 2>/dev/null; then
                return 0
            fi
            sleep 0.5
        done
        kill -TERM "${CHILD_PID}" 2>/dev/null || true
        sleep 3
        kill -KILL "${CHILD_PID}" 2>/dev/null || true
    fi
}

on_signal() {
    local sig="$1"
    trap - TERM INT
    log "Received ${sig}; terminating part_${PART_PADDED}"
    terminate_child
    printf '%s\n' "$(date '+%Y-%m-%d %H:%M:%S') signal=${sig}" > "${FAILED_MARKER}"
    exit 143
}

trap 'on_signal TERM' TERM
trap 'on_signal INT' INT

if [[ -f "${DONE_MARKER}" ]]; then
    log "part_${PART_PADDED} already complete; skipping"
    exit 0
fi

rm -f "${FAILED_MARKER}"

export PYTHONPATH="${PROJECT_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
export DESKSHOT_LSF_PART="${PART}"
export DESKSHOT_LSF_OUTPUT_ROOT="${OUTPUT_ROOT}"
# Without this, scene-batch stdout sits in a block buffer until the job exits,
# so a running or hung part looks identical to one that has produced nothing.
export PYTHONUNBUFFERED=1

cd "${PROJECT_ROOT}"

if [[ "$(normalize_bool "${DESKSHOT_SKIP_PREFLIGHT:-false}")" != "true" ]]; then
    log "Preflight part_${PART_PADDED} host=$(hostname) apps=${APP_ALLOWLIST:-default}"
    PREFLIGHT_CMD=(python -m deskshot.cli preflight --strict-compute)
    if [[ -n "${APP_ALLOWLIST}" ]]; then
        PREFLIGHT_CMD+=(--apps "${APP_ALLOWLIST}")
    fi
    set +e
    "${PREFLIGHT_CMD[@]}" > >(tee -a "${RUN_LOG}") 2>&1
    PREFLIGHT_RC=$?
    set -e
    if [[ "${PREFLIGHT_RC}" -ne 0 ]]; then
        # A node-local infrastructure problem is not a defect in this part. If we
        # mark it failed, the default RESUBMIT_FAILED=false means it is never
        # retried and the part is silently lost - observed at 8 jobs, where three
        # landed on one node with a full disk. Leave no marker so the submitter
        # picks it up again, most likely on a different host, bounded by
        # MAX_TRANSIENT_RETRIES so a genuinely broken setup still terminates.
        if grep -qiE "No space left on device|Errno 28|Read-only file system|Cannot allocate memory|Errno 12" "${RUN_LOG}" 2>/dev/null; then
            attempts=0
            if [[ -f "${RETRY_MARKER}" ]]; then
                attempts="$(cat "${RETRY_MARKER}" 2>/dev/null || echo 0)"
            fi
            attempts=$((attempts + 1))
            printf '%s' "${attempts}" > "${RETRY_MARKER}"
            if [[ "${attempts}" -lt "${MAX_TRANSIENT_RETRIES}" ]]; then
                log "part_${PART_PADDED} preflight_transient host=$(hostname) attempt=${attempts}/${MAX_TRANSIENT_RETRIES}; leaving part unmarked for retry"
                exit "${PREFLIGHT_RC}"
            fi
            log "part_${PART_PADDED} transient_retries_exhausted attempts=${attempts}"
        fi
        log "part_${PART_PADDED} preflight_failed rc=${PREFLIGHT_RC} host=$(hostname)"
        printf '%s preflight_failed host=%s rc=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$(hostname)" "${PREFLIGHT_RC}" > "${FAILED_MARKER}"
        exit "${PREFLIGHT_RC}"
    fi
else
    log "Preflight skipped for part_${PART_PADDED}"
fi

if [[ "${PARALLEL_WORKERS}" -gt 1 && "${SESSION_GROUP_SIZE}" -gt 1 ]]; then
    log "WARN session_group_size=${SESSION_GROUP_SIZE} is incompatible with parallel_workers=${PARALLEL_WORKERS}; forcing session_group_size=1"
    SESSION_GROUP_SIZE=1
fi

CMD=(
    python -m deskshot.cli scene-batch
    --start-seed "${START_SEED}"
    --count "${COUNT}"
    --output "${JOB_DIR}"
    --parallel-workers "${PARALLEL_WORKERS}"
    --base-display-number "${BASE_DISPLAY_NUMBER}"
    --max-attempts "${MAX_ATTEMPTS}"
    --session-group-size "${SESSION_GROUP_SIZE}"
)

if [[ "${INCLUDE_DESKTOP_CHROME}" == "true" ]]; then
    CMD+=(--include-chrome)
else
    CMD+=(--no-include-chrome)
fi

if [[ -n "${APP_ALLOWLIST}" ]]; then
    CMD+=(--apps "${APP_ALLOWLIST}")
fi

log "Starting part_${PART_PADDED} start_seed=${START_SEED} count=${COUNT} parallel_workers=${PARALLEL_WORKERS} session_group_size=${SESSION_GROUP_SIZE} base_display=${BASE_DISPLAY_NUMBER} desktop_chrome=${INCLUDE_DESKTOP_CHROME} apps=${APP_ALLOWLIST:-default}"
set +e
"${CMD[@]}" > >(tee -a "${RUN_LOG}") 2>&1 &
CHILD_PID=$!
wait "${CHILD_PID}"
CMD_RC=$?
CHILD_PID=""
set -e

if [[ "${CMD_RC}" -eq 0 ]]; then
    printf '%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" > "${DONE_MARKER}"
    rm -f "${FAILED_MARKER}"
    if [[ "${MERGE_EACH_JOB}" == "true" ]]; then
        if command -v flock >/dev/null 2>&1; then
            flock "${MERGE_LOCK}" python "${SCRIPT_DIR}/merge_scene_batch_outputs.py" --output-root "${OUTPUT_ROOT}" || log "WARN per-job merge failed"
        else
            python "${SCRIPT_DIR}/merge_scene_batch_outputs.py" --output-root "${OUTPUT_ROOT}" || log "WARN per-job merge failed"
        fi
    else
        log "Skipping per-job merge; submitter/final merge owns merged index"
    fi
    log "part_${PART_PADDED} completed"
    exit 0
fi

printf '%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" > "${FAILED_MARKER}"
log "part_${PART_PADDED} failed rc=${CMD_RC}"
exit 1

#!/usr/bin/env bash
# Resumable LSF submitter for DeskShot scene-batch jobs.
#
# Usage:
#   bash scripts/submit_scene_batch_jobs.sh [parallel_workers_per_job] [scenes_per_job] \
#       [output_root] [start_part] [end_part] [start_seed] [app_allowlist]
# Optional env:
#   SESSION_GROUP_SIZE_PER_JOB=N enables same-session reuse for single-worker jobs.

set -euo pipefail

PARALLEL_WORKERS_PER_JOB="${1:-${PARALLEL_WORKERS_PER_JOB:-1}}"
SCENES_PER_JOB="${2:-${SCENES_PER_JOB:-2}}"
OUTPUT_ROOT="${3:-${OUTPUT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/data_lsf_scene_batch}}"
START_PART="${4:-${START_PART:-0}}"
END_PART="${5:-${END_PART:-3}}"
START_SEED="${6:-${START_SEED:-0}}"
APP_ALLOWLIST="${7:-${APP_ALLOWLIST:-}}"

MAX_CONCURRENT="${MAX_CONCURRENT:-2}"
POLL_INTERVAL="${POLL_INTERVAL:-30}"
RUN_ID_RAW="${RUN_ID:-$(date '+%Y%m%d%H%M%S')_$$}"
RUN_ID="$(printf '%s' "${RUN_ID_RAW}" | tr -c 'A-Za-z0-9_' '_' | cut -c1-48)"
JOB_PREFIX="${JOB_PREFIX:-dsd_${RUN_ID}_part}"
MEM_GB="${MEM_GB:-32}"
LSF_CORES="${LSF_CORES:-${PARALLEL_WORKERS_PER_JOB}}"
JOB_GROUP="${JOB_GROUP:-}"   # optional LSF user group (-G)
QUEUE="${QUEUE:-}"
WALL_TIME="${WALL_TIME:-3:00}"
# Controls desktop chrome extraction (desktop icons, panels, dock). Browser
# app selection is controlled separately by APP_ALLOWLIST.
# Accept 1/yes/on as well as true, so documented DRY_RUN=1 style values are not
# silently read as false (which previously submitted real jobs during a dry run
# and dropped desktop chrome from submitted parts).
normalize_bool() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) printf 'true' ;;
        *) printf 'false' ;;
    esac
}

INCLUDE_DESKTOP_CHROME="$(normalize_bool "${INCLUDE_DESKTOP_CHROME:-${INCLUDE_CHROME:-true}}")"
SESSION_GROUP_SIZE_PER_JOB="${SESSION_GROUP_SIZE_PER_JOB:-1}"
# Seeds a part may burn while rejecting near-duplicate plans. This is both the
# attempt budget passed to the job and the size of the part's seed block, so the
# two can never disagree and parts can never overlap.
SEED_ATTEMPT_MULTIPLIER="${SEED_ATTEMPT_MULTIPLIER:-20}"
MAX_ATTEMPTS_PER_JOB="${MAX_ATTEMPTS_PER_JOB:-$((SCENES_PER_JOB * SEED_ATTEMPT_MULTIPLIER))}"
SEED_STRIDE_PER_PART="${SEED_STRIDE_PER_PART:-${MAX_ATTEMPTS_PER_JOB}}"
DRY_RUN="$(normalize_bool "${DRY_RUN:-false}")"
RESUBMIT_FAILED="$(normalize_bool "${RESUBMIT_FAILED:-false}")"
DISPLAY_OFFSET="${DISPLAY_OFFSET:-400}"
DISPLAY_STRIDE="${DISPLAY_STRIDE:-32}"
RUN_DISPLAY_STRIDE="${RUN_DISPLAY_STRIDE:-4096}"
# Keep automatic display IDs below conservative X client limits while still
# separating common concurrent runs by enough room for 128 part slots.
RUN_DISPLAY_SLOT_MOD="${RUN_DISPLAY_SLOT_MOD:-12}"
RUN_DISPLAY_SLOT="${RUN_DISPLAY_SLOT:-$(printf '%s' "${RUN_ID}" | cksum | awk -v mod="${RUN_DISPLAY_SLOT_MOD}" '{print $1 % mod}')}"
RUN_DISPLAY_OFFSET="${RUN_DISPLAY_OFFSET:-$((RUN_DISPLAY_SLOT * RUN_DISPLAY_STRIDE))}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOG_DIR="${OUTPUT_ROOT}/.lsbatch"
DONE_DIR="${LOG_DIR}/parts_done"
FAILED_DIR="${LOG_DIR}/parts_failed"
SUBMIT_LOG="${LOG_DIR}/submit.log"
JOB_SCRIPT="${SCRIPT_DIR}/run_scene_batch_lsf_job.sh"

mkdir -p "${LOG_DIR}" "${DONE_DIR}" "${FAILED_DIR}" "${OUTPUT_ROOT}/jobs"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "${SUBMIT_LOG}"
}

count_active_jobs() {
    if [[ "${DRY_RUN}" == "true" ]]; then
        echo 0
        return 0
    fi
    local out
    if ! out="$(bjobs -o "jobid" -noheader -J "${JOB_PREFIX}_*" 2>/dev/null)"; then
        echo "${MAX_CONCURRENT}"
        return 0
    fi
    if [[ -z "${out//[[:space:]]/}" ]]; then
        echo 0
        return 0
    fi
    printf "%s\n" "${out}" | wc -l
}

list_active_parts() {
    if [[ "${DRY_RUN}" == "true" ]]; then
        return 0
    fi
    local out
    if ! out="$(bjobs -o "job_name" -noheader -J "${JOB_PREFIX}_*" 2>/dev/null)"; then
        return 0
    fi
    if [[ -z "${out//[[:space:]]/}" ]]; then
        return 0
    fi
    printf "%s\n" "${out}" | while read -r name; do
        if [[ "${name}" == "${JOB_PREFIX}_"* ]]; then
            suffix="${name#${JOB_PREFIX}_}"
            if [[ "${suffix}" =~ ^[0-9]+$ ]]; then
                echo "$((10#${suffix}))"
            fi
        fi
    done | sort -n | uniq
}

declare -A active_map=()
declare -A done_map=()
declare -A failed_map=()

TOTAL_PARTS=$((END_PART - START_PART + 1))
failed_count=0
log "submitter_start run_id=${RUN_ID} job_prefix=${JOB_PREFIX} workers_per_job=${PARALLEL_WORKERS_PER_JOB} session_group_size=${SESSION_GROUP_SIZE_PER_JOB} scenes_per_job=${SCENES_PER_JOB} max_attempts_per_job=${MAX_ATTEMPTS_PER_JOB} seed_stride=${SEED_STRIDE_PER_PART} parts=${START_PART}-${END_PART} start_seed=${START_SEED} max_concurrent=${MAX_CONCURRENT} display_offset=${DISPLAY_OFFSET}+${RUN_DISPLAY_OFFSET} desktop_chrome=${INCLUDE_DESKTOP_CHROME} resubmit_failed=${RESUBMIT_FAILED} apps=${APP_ALLOWLIST:-default}"

while true; do
    active_map=()
    while read -r active_part; do
        [[ -z "${active_part}" ]] && continue
        active_map["${active_part}"]=1
    done < <(list_active_parts)

    done_map=()
    failed_map=()
    done_count=0
    failed_count=0
    for ((part=START_PART; part<=END_PART; part++)); do
        padded="$(printf '%04d' "${part}")"
        if [[ -f "${DONE_DIR}/part_${padded}.done" ]]; then
            done_map["${part}"]=1
            done_count=$((done_count + 1))
        elif [[ "${RESUBMIT_FAILED}" != "true" && -f "${FAILED_DIR}/part_${padded}.failed" ]]; then
            failed_map["${part}"]=1
            failed_count=$((failed_count + 1))
        fi
    done

    if [[ "$((done_count + failed_count))" -ge "${TOTAL_PARTS}" ]]; then
        log "all_parts_terminal done=${done_count}/${TOTAL_PARTS} failed=${failed_count}/${TOTAL_PARTS}"
        break
    fi

    running="$(count_active_jobs)"
    slots=$((MAX_CONCURRENT - running))
    if [[ "${slots}" -le 0 ]]; then
        log "waiting active_jobs=${running}/${MAX_CONCURRENT} done=${done_count}/${TOTAL_PARTS}"
        sleep "${POLL_INTERVAL}"
        continue
    fi

    submitted_now=0
    for ((part=START_PART; part<=END_PART; part++)); do
        if [[ -n "${done_map[${part}]:-}" || -n "${failed_map[${part}]:-}" || -n "${active_map[${part}]:-}" ]]; then
            continue
        fi

        padded="$(printf '%04d' "${part}")"
        # Stride by the attempt budget, not by SCENES_PER_JOB. A part advances its
        # seed cursor once per rejected plan, bounded by MAX_ATTEMPTS_PER_JOB, so a
        # block that size is exactly what it can consume. Striding by SCENES_PER_JOB
        # let any part that rejected a scene walk into the next part's seeds and
        # regenerate identical scenes.
        part_start_seed=$((START_SEED + (part - START_PART) * SEED_STRIDE_PER_PART))
        base_display=$((DISPLAY_OFFSET + RUN_DISPLAY_OFFSET + part * DISPLAY_STRIDE))
        job_name="${JOB_PREFIX}_${part}"
        bsub_cmd=(
            bsub
            -J "${job_name}"
            -n "${LSF_CORES}"
            -R "rusage[mem=${MEM_GB}G]"
            -oo "${LOG_DIR}/${job_name}.%J.stdout"
            -eo "${LOG_DIR}/${job_name}.%J.stderr"
        )
        if [[ -n "${QUEUE}" ]]; then
            bsub_cmd+=(-q "${QUEUE}")
        fi
        if [[ -n "${JOB_GROUP}" ]]; then
            bsub_cmd+=(-G "${JOB_GROUP}")
        fi
        if [[ -n "${WALL_TIME}" ]]; then
            bsub_cmd+=(-W "${WALL_TIME}")
        fi
        bsub_cmd+=(
            /bin/bash "${JOB_SCRIPT}"
            "${OUTPUT_ROOT}" "${part}" "${part_start_seed}" "${SCENES_PER_JOB}"
            "${PARALLEL_WORKERS_PER_JOB}" "${base_display}" "${MAX_ATTEMPTS_PER_JOB}" "${INCLUDE_DESKTOP_CHROME}" "${APP_ALLOWLIST}" "${SESSION_GROUP_SIZE_PER_JOB}"
        )

        if [[ "${DRY_RUN}" == "true" ]]; then
            printf 'DRY_RUN'
            printf ' %q' "${bsub_cmd[@]}"
            printf '\n'
            log "dry_run part=${part} start_seed=${part_start_seed} base_display=${base_display} job_name=${job_name}"
            submitted_now=$((submitted_now + 1))
            active_map["${part}"]=1
            slots=$((slots - 1))
        elif "${bsub_cmd[@]}" >> "${SUBMIT_LOG}" 2>&1; then
            submitted_now=$((submitted_now + 1))
            active_map["${part}"]=1
            slots=$((slots - 1))
            log "submitted part=${part} start_seed=${part_start_seed} base_display=${base_display}"
        else
            log "WARN bsub_failed part=${part}"
            break
        fi

        if [[ "${slots}" -le 0 ]]; then
            break
        fi
    done

    if [[ "${submitted_now}" -eq 0 ]]; then
        log "waiting done=${done_count}/${TOTAL_PARTS} failed=${failed_count}/${TOTAL_PARTS} active_jobs=${running}"
    fi
    if [[ "${DRY_RUN}" == "true" ]]; then
        log "dry_run_done submitted=${submitted_now}"
        exit 0
    fi
    sleep "${POLL_INTERVAL}"
done

if [[ "${DRY_RUN}" != "true" ]]; then
    python "${SCRIPT_DIR}/merge_scene_batch_outputs.py" --output-root "${OUTPUT_ROOT}" >> "${SUBMIT_LOG}" 2>&1 || true
fi
if [[ "${failed_count}" -gt 0 ]]; then
    log "submitter_done_with_failures failed=${failed_count}/${TOTAL_PARTS}; remove failed markers or set RESUBMIT_FAILED=true to retry"
    exit 1
fi
log "submitter_done"

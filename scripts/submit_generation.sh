#!/usr/bin/env bash
# Submit a corpus run to LSF as one job array, one array element per plan shard.
#
# Why an array rather than N separate submissions: LSF gives each element
# $LSB_JOBINDEX, which is exactly the shard index, so the mapping from job to
# work is arithmetic rather than bookkeeping. And because every element runs with
# --resume, a preempted or killed element can simply be resubmitted: it skips
# what is already on disk and continues. That is what makes the `preemptable`
# queue usable for a multi-day run.
#
#   scripts/submit_generation.sh \
#       --plan   /proj/.../corpus/v1/plan/corpus.jsonl \
#       --root   /proj/.../corpus/v1 \
#       --shards 64 --cores 24 --workers 6 --queue normal
#
# One shard = one directory = one writer. Two elements never write the same
# directory, so nothing can be overwritten and no two jobs contend on a file.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLAN=""; ROOT=""; SHARDS=64; CORES=24; WORKERS=6; QUEUE="normal"
WALLTIME=""; JOBNAME="deskshot"; DRYRUN=0
# This cluster's esub requires a fairshare group on every submission.
GROUP="${LSB_DEFAULT_USERGROUP:-}"   # optional LSF user group (-G)
DISTRIBUTE=0
# One independent job per shard, named "<jobname>-<index>", with no host named.
#
# An array element cannot be resubmitted on its own, and a lost shard is the
# normal failure of a long run - so `scripts/supervise_generation.py` needs each
# shard to be its own job with the index in its name. Unlike --distribute this
# does *not* pin a host: bounding sessions per node is already done by the core
# reservation, and pinning left four shards pending six hours behind an advance
# reservation on the previous run.
PER_SHARD=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --plan)     PLAN="$2"; shift 2 ;;
    --root)     ROOT="$2"; shift 2 ;;
    --shards)   SHARDS="$2"; shift 2 ;;
    --cores)    CORES="$2"; shift 2 ;;
    --workers)  WORKERS="$2"; shift 2 ;;
    --queue)    QUEUE="$2"; shift 2 ;;
    --walltime) WALLTIME="$2"; shift 2 ;;
    --name)     JOBNAME="$2"; shift 2 ;;
    --group)    GROUP="$2"; shift 2 ;;
    --distribute) DISTRIBUTE=1; shift ;;
    --per-shard)  PER_SHARD=1; shift ;;
    --dry-run)  DRYRUN=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$PLAN" && -f "$PLAN" ]] || { echo "need --plan pointing at a scene-plan file" >&2; exit 2; }
[[ -n "$ROOT" ]] || { echo "need --root" >&2; exit 2; }

# --- inotify headroom -------------------------------------------------------
# `fs.inotify.max_user_instances` is per user *per node*, and every desktop
# session spends several: Xvfb, the window manager, the panel, the file-manager
# desktop and each app. Exceed it and GIO reports "Unable to find default local
# file monitor type", which kills any app that watches a file - measured as
# 8,920 launch failures in one run, all of them xarchiver, pluma and
# gnome-system-monitor, while the same apps were flawless at eight sessions.
#
# LSF packs as many jobs onto a node as its cores allow, so the number that
# matters is not workers-per-job but **sessions per node**, and that is decided
# by how many cores each job reserves. Refuse to submit a configuration that
# cannot fit.
INSTANCES=$(cat /proc/sys/fs/inotify/max_user_instances 2>/dev/null || echo 128)
NODE_CORES=${DESKSHOT_NODE_CORES:-96}
# With --distribute there is exactly one job per host by construction.
if [[ "$DISTRIBUTE" == "1" ]]; then JOBS_PER_NODE=1
else JOBS_PER_NODE=$(( NODE_CORES / CORES )); [[ $JOBS_PER_NODE -lt 1 ]] && JOBS_PER_NODE=1; fi
SESSIONS_PER_NODE=$(( JOBS_PER_NODE * WORKERS ))
# Measured headroom: a session costs about five inotify instances.
BUDGET=$(( INSTANCES / 5 ))
echo "inotify: ${INSTANCES} instances/node, ~5 per session -> at most ${BUDGET} sessions"
echo "planned: ${JOBS_PER_NODE} job(s) x ${WORKERS} workers = ${SESSIONS_PER_NODE} sessions per ${NODE_CORES}-core node"
if [[ $SESSIONS_PER_NODE -gt $BUDGET ]]; then
  NEED=$(( NODE_CORES * WORKERS / BUDGET ))
  echo >&2
  echo "REFUSING: ${SESSIONS_PER_NODE} sessions per node exceeds the ${BUDGET} that" >&2
  echo "fs.inotify.max_user_instances allows. Apps that watch files will die at" >&2
  echo "startup and the run will look fine while losing a third of its samples." >&2
  echo >&2
  echo "Fix by reserving more of each node so fewer jobs share it:" >&2
  echo "  --cores ${NEED}   (with --workers ${WORKERS})" >&2
  echo "or lower --workers. Override with DESKSHOT_ALLOW_INOTIFY_OVERSUBSCRIBE=1." >&2
  [[ "${DESKSHOT_ALLOW_INOTIFY_OVERSUBSCRIBE:-0}" == "1" ]] || exit 2
fi

mkdir -p "$ROOT"/{logs,status,shards}

# The run manifest. Written before anything is submitted, so what was asked for
# is recorded even if the submission fails.
cat > "$ROOT/run.json" <<JSON
{
  "plan": "$PLAN",
  "root": "$ROOT",
  "shards": $SHARDS,
  "cores_per_job": $CORES,
  "workers_per_job": $WORKERS,
  "queue": "$QUEUE",
  "commit": "$(cd "$PROJECT_ROOT" && git -c safe.directory='*' rev-parse HEAD 2>/dev/null || echo unknown)",
  "planned_scenes": $(wc -l < "$PLAN"),
  "submitted_by": "$(id -un)",
  "user_group": "$GROUP"
}
JSON

RUNNER="$ROOT/run_shard.sh"
cat > "$RUNNER" <<'RUNNER_EOF'
#!/usr/bin/env bash
# One array element: take shard (LSB_JOBINDEX - 1) of the plan and run it.
set -euo pipefail
PROJECT_ROOT="__PROJECT_ROOT__"
PLAN="__PLAN__"; ROOT="__ROOT__"; SHARDS=__SHARDS__; WORKERS=__WORKERS__

IDX=${1:-$(( ${LSB_JOBINDEX:-1} - 1 ))}
SHARD_DIR="$ROOT/shards/$(printf 'shard-%04d' "$IDX")"
mkdir -p "$SHARD_DIR"

cd "$PROJECT_ROOT"
export PYTHONPATH="$PROJECT_ROOT/src"
# Route captures into the sharded tree, and skip the six debug renders - they
# are derived, and they are 65% of a sample's bytes.
export DESKSHOT_CORPUS_LAYOUT=1
export DESKSHOT_SKIP_VIZ=1
# Node-local: an X display is a per-machine resource and so is its lease.
export DESKSHOT_LEASE_DIR="${TMPDIR:-/tmp}/deskshot_displays"

# Reclaim anything a previous element leaked on this node before taking displays.
python scripts/cleanup_stale_sessions.py --apply >/dev/null 2>&1 || true

# The marker is what tells the supervisor this shard is finished rather than
# merely not running. Written only on a clean exit, so a shard killed by a
# walltime, a node eviction or an OOM has no marker and is resubmitted; because
# every element runs with --resume, the resubmission skips what is already on
# disk instead of redoing it.
# Markers live under the job name, so two runs into one corpus cannot read each
# other's completions. Sharing `status/done` meant a second run started with 300
# markers already present and a supervisor would have called it finished before
# it began.
DONE_MARKER="$ROOT/status/done/__JOBNAME__/$(printf 'shard-%04d' "$IDX")"
mkdir -p "$(dirname "$DONE_MARKER")"

# `set -e` would abort here before the status could be read, and the shard
# would then be indistinguishable from one that finished.
set +e
python scripts/lease_display.py --count "$WORKERS" -- \
  env PYTHONPATH="$PROJECT_ROOT/src" \
  python -m deskshot.cli scene-batch \
    --plan "$PLAN" --shard "$IDX/$SHARDS" --resume \
    --output "$SHARD_DIR" \
    --parallel-workers "$WORKERS" \
    --base-display-number '{display}' \
    --scene-timeout 1200 --include-chrome
STATUS=$?
set -e

if [[ $STATUS -eq 0 ]]; then
  date -u +%Y-%m-%dT%H:%M:%SZ > "$DONE_MARKER"
fi
exit $STATUS
RUNNER_EOF

sed -i "s|__PROJECT_ROOT__|$PROJECT_ROOT|; s|__PLAN__|$PLAN|; s|__ROOT__|$ROOT|; \
        s|__SHARDS__|$SHARDS|; s|__WORKERS__|$WORKERS|; s|__JOBNAME__|$JOBNAME|" "$RUNNER"
chmod +x "$RUNNER"

BSUB_ARGS=(-J "${JOBNAME}[1-${SHARDS}]" -n "$CORES" -q "$QUEUE" ${GROUP:+-G "$GROUP"}
           -R "span[hosts=1]"
           -o "$ROOT/logs/%J.%I.out" -e "$ROOT/logs/%J.%I.err")
[[ -n "$WALLTIME" ]] && BSUB_ARGS+=(-W "$WALLTIME")

# One job per host, by name. The alternative - reserving most of a node so LSF
# cannot pack a second job onto it - works but wastes the cores it reserves. The
# constraint is not cores, it is that `fs.inotify.max_user_instances` is per user
# per node, so what has to be controlled is *how many of our jobs share a host*.
# Naming the host does that exactly, and leaves the rest of the node free for
# everyone else.
HOSTS=()
if [[ "$DISTRIBUTE" == "1" ]]; then
  mapfile -t HOSTS < <(
    bhosts -w 2>/dev/null | awk -v need="$CORES" '
      NR > 1 && $2 == "ok" {
        free = $4 - $5
        if (free >= need) print $1, free
      }' | sort -k2 -nr | awk '{print $1}' | head -n "$SHARDS"
  )
  if [[ ${#HOSTS[@]} -lt $SHARDS ]]; then
    echo "only ${#HOSTS[@]} hosts have ${CORES} free slots; asked for ${SHARDS}" >&2
    [[ ${#HOSTS[@]} -eq 0 ]] && exit 2
    SHARDS=${#HOSTS[@]}
    echo "reducing to ${SHARDS} shards, one per host" >&2
  fi
  echo "distributing ${SHARDS} shards over ${SHARDS} distinct hosts"
fi

if [[ "$PER_SHARD" == "1" && "$DISTRIBUTE" == "1" ]]; then
  echo "--per-shard and --distribute are alternatives; pick one" >&2; exit 2
fi

echo "submitting ${SHARDS} shards x ${WORKERS} workers on ${CORES} cores, queue ${QUEUE}"
echo "  plan:  $PLAN ($(wc -l < "$PLAN") scenes)"
echo "  root:  $ROOT"
if [[ "$DISTRIBUTE" == "1" ]]; then
  if [[ "$DRYRUN" == "1" ]]; then
    echo "DRY RUN - would submit ${SHARDS} jobs, e.g.:"
    echo "  bsub -J ${JOBNAME}-0 -n $CORES -q $QUEUE ${GROUP:+-G $GROUP} -m ${HOSTS[0]} $RUNNER 0"
    printf '  hosts: %s ...\n' "${HOSTS[*]:0:6}"
    exit 0
  fi
  for (( i=0; i<SHARDS; i++ )); do
    bsub -J "${JOBNAME}-${i}" -n "$CORES" -q "$QUEUE" ${GROUP:+-G "$GROUP"} \
         -R "span[hosts=1]" -m "${HOSTS[$i]}" \
         -o "$ROOT/logs/${JOBNAME}-${i}.%J.out" -e "$ROOT/logs/${JOBNAME}-${i}.%J.err" \
         ${WALLTIME:+-W "$WALLTIME"} \
         "$RUNNER" "$i" > /dev/null || echo "submit failed for shard $i on ${HOSTS[$i]}" >&2
  done
  echo "submitted ${SHARDS} jobs, one per host"
elif [[ "$PER_SHARD" == "1" ]]; then
  if [[ "$DRYRUN" == "1" ]]; then
    echo "DRY RUN - would submit ${SHARDS} independent jobs, e.g.:"
    echo "  bsub -J ${JOBNAME}-0 -n $CORES -q $QUEUE ${GROUP:+-G $GROUP} -R span[hosts=1] $RUNNER 0"
    echo "  (no host named - LSF places them, the core reservation bounds packing)"
    exit 0
  fi
  FAILED=0
  for (( i=0; i<SHARDS; i++ )); do
    bsub -J "${JOBNAME}-${i}" -n "$CORES" -q "$QUEUE" ${GROUP:+-G "$GROUP"} \
         -R "span[hosts=1]" \
         -o "$ROOT/logs/${JOBNAME}-${i}.%J.out" -e "$ROOT/logs/${JOBNAME}-${i}.%J.err" \
         ${WALLTIME:+-W "$WALLTIME"} \
         "$RUNNER" "$i" > /dev/null || { echo "submit failed for shard $i" >&2; FAILED=$((FAILED+1)); }
  done
  echo "submitted $(( SHARDS - FAILED )) of ${SHARDS} jobs, one per shard"
  [[ $FAILED -gt 0 ]] && echo "${FAILED} failed - the supervisor will pick them up" >&2
else
if [[ "$DRYRUN" == "1" ]]; then
  echo "DRY RUN - would run: bsub ${BSUB_ARGS[*]} $RUNNER"
  exit 0
fi
bsub "${BSUB_ARGS[@]}" "$RUNNER"
fi
echo
echo "watch it with:"
echo "  PYTHONPATH=src python scripts/watch_generation.py --root $ROOT --watch"

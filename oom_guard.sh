#!/usr/bin/env bash
# OOM Guard: kills training process before pod crash when memory exceeds threshold
# Usage: bash oom_guard.sh [threshold_pct] [check_interval_sec]
#
# Run alongside training:
#   bash oom_guard.sh 96 5 &

THRESHOLD=${1:-96}
INTERVAL=${2:-5}
LOGFILE="/fsx-shared/juncheng/EHR/oom_guard.log"

echo "=== OOM Guard Started ===" | tee "$LOGFILE"
echo "Threshold: ${THRESHOLD}% of cgroup limit" | tee -a "$LOGFILE"
echo "Check interval: ${INTERVAL}s" | tee -a "$LOGFILE"
echo "Started: $(date -u '+%Y-%m-%d %H:%M:%S UTC')" | tee -a "$LOGFILE"
echo "========================================" | tee -a "$LOGFILE"

while true; do
    CGROUP_USAGE=$(cat /sys/fs/cgroup/memory.current 2>/dev/null || echo 0)
    CGROUP_LIMIT=$(cat /sys/fs/cgroup/memory.max 2>/dev/null || echo 0)

    if [ "$CGROUP_LIMIT" -eq 0 ]; then
        sleep "$INTERVAL"
        continue
    fi

    # Use integer math to avoid python dependency: pct = usage * 100 / limit
    PCT=$((CGROUP_USAGE * 100 / CGROUP_LIMIT))

    if [ "$PCT" -ge "$THRESHOLD" ]; then
        USAGE_GB=$((CGROUP_USAGE / 1073741824))
        LIMIT_GB=$((CGROUP_LIMIT / 1073741824))
        MSG="[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] KILLING TRAINING: ${USAGE_GB}GB / ${LIMIT_GB}GB (${PCT}%) >= ${THRESHOLD}% threshold"
        echo "$MSG" | tee -a "$LOGFILE"

        # Kill torchrun and all child python processes gracefully first
        pkill -TERM -f torchrun 2>/dev/null
        sleep 3
        # Force kill if still running
        pkill -KILL -f torchrun 2>/dev/null
        pkill -KILL -f "verl.trainer.sft_trainer" 2>/dev/null

        echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] Training processes killed. Pod saved." | tee -a "$LOGFILE"
        exit 0
    fi

    sleep "$INTERVAL"
done

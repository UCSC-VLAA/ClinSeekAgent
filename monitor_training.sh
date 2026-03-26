#!/usr/bin/env bash
# System resource monitor for training jobs
# Logs CPU memory, GPU memory, and process info every INTERVAL seconds
# Usage: bash monitor_training.sh [interval_seconds] [logfile]
#
# Run alongside training:
#   bash monitor_training.sh 30 training_system_monitor.log &

INTERVAL=${1:-30}
LOGFILE=${2:-/fsx-shared/juncheng/EHR/training_system_monitor.log}

echo "=== Training System Monitor ===" | tee "$LOGFILE"
echo "Started: $(date -u '+%Y-%m-%d %H:%M:%S UTC')" | tee -a "$LOGFILE"
echo "Interval: ${INTERVAL}s" | tee -a "$LOGFILE"
echo "Pod memory limit: $(python3 -c "print(f'{int(open(\"/sys/fs/cgroup/memory.max\").read().strip()) / 1024**3:.0f} GB')" 2>/dev/null || echo 'unknown')" | tee -a "$LOGFILE"
echo "========================================" | tee -a "$LOGFILE"

while true; do
    {
        echo ""
        echo "--- $(date -u '+%Y-%m-%d %H:%M:%S UTC') ---"

        # Cgroup memory (what k8s sees)
        CGROUP_USAGE=$(cat /sys/fs/cgroup/memory.current 2>/dev/null || echo 0)
        CGROUP_LIMIT=$(cat /sys/fs/cgroup/memory.max 2>/dev/null || echo 0)
        CGROUP_GB=$(python3 -c "print(f'{${CGROUP_USAGE} / 1024**3:.1f}')")
        CGROUP_LIMIT_GB=$(python3 -c "print(f'{${CGROUP_LIMIT} / 1024**3:.0f}')")
        CGROUP_PCT=$(python3 -c "print(f'{${CGROUP_USAGE} / ${CGROUP_LIMIT} * 100:.1f}')")
        echo "CGROUP_MEM: ${CGROUP_GB} / ${CGROUP_LIMIT_GB} GB (${CGROUP_PCT}%)"

        # System memory
        free -h | head -2

        # Count python3 processes and their total RSS
        NPROCS=$(pgrep -c python3 2>/dev/null || echo 0)
        TOTAL_RSS=$(ps -eo rss,comm | grep python3 | awk '{sum+=$1} END {printf "%.1f", sum/1024/1024}')
        echo "PYTHON3_PROCS: ${NPROCS}, TOTAL_RSS: ${TOTAL_RSS} GB"

        # Top 5 python3 processes by memory
        echo "TOP_PROCS:"
        ps -eo pid,rss,vsz,comm --sort=-rss | grep python3 | head -5 | awk '{printf "  PID=%s RSS=%.1fGB VSZ=%.1fGB\n", $1, $2/1024/1024, $3/1024/1024}'

        # GPU memory
        nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits 2>/dev/null | while IFS=', ' read -r idx used total util; do
            echo "GPU${idx}: ${used}/${total} MiB (${util}% util)"
        done

        # Warning at multiple thresholds
        if python3 -c "exit(0 if ${CGROUP_USAGE} / ${CGROUP_LIMIT} > 0.90 else 1)" 2>/dev/null; then
            echo "CRITICAL: Memory usage above 90% of pod limit! OOM imminent!"
        elif python3 -c "exit(0 if ${CGROUP_USAGE} / ${CGROUP_LIMIT} > 0.80 else 1)" 2>/dev/null; then
            echo "WARNING: Memory usage above 80% of pod limit!"
        elif python3 -c "exit(0 if ${CGROUP_USAGE} / ${CGROUP_LIMIT} > 0.70 else 1)" 2>/dev/null; then
            echo "NOTICE: Memory usage above 70% of pod limit."
        fi

    } >> "$LOGFILE" 2>&1

    sleep "$INTERVAL"
done

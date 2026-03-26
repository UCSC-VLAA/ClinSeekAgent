#!/usr/bin/env bash
# Enhanced system resource monitor for training jobs with checkpoint save detection
# Logs CPU memory, GPU memory, and process info with adaptive interval
# Detects checkpoint save phases and increases sampling rate
# Usage: bash monitor_training_enhanced.sh [base_interval] [training_logfile] [output_logfile]
#
# Run alongside training:
#   bash monitor_training_enhanced.sh 10 training_v2.log training_system_monitor.log &

BASE_INTERVAL=${1:-10}
TRAINING_LOG=${2:-/fsx-shared/juncheng/EHR/training_v2.log}
LOGFILE=${3:-/fsx-shared/juncheng/EHR/training_system_monitor.log}

# Adaptive interval: faster during checkpoint save
CHECKPOINT_INTERVAL=2
VALIDATION_INTERVAL=5

# Memory growth tracking
PREV_CGROUP_USAGE=0
PREV_TIMESTAMP=0

echo "=== Enhanced Training System Monitor ===" | tee "$LOGFILE"
echo "Started: $(date -u '+%Y-%m-%d %H:%M:%S UTC')" | tee -a "$LOGFILE"
echo "Base interval: ${BASE_INTERVAL}s" | tee -a "$LOGFILE"
echo "Checkpoint save interval: ${CHECKPOINT_INTERVAL}s" | tee -a "$LOGFILE"
echo "Validation interval: ${VALIDATION_INTERVAL}s" | tee -a "$LOGFILE"
echo "Training log: ${TRAINING_LOG}" | tee -a "$LOGFILE"
echo "Pod memory limit: $(python3 -c "print(f'{int(open(\"/sys/fs/cgroup/memory.max\").read().strip()) / 1024**3:.0f} GB')" 2>/dev/null || echo 'unknown')" | tee -a "$LOGFILE"
echo "========================================" | tee -a "$LOGFILE"

# Function to detect training phase
detect_phase() {
    # Check last 20 lines of training log for current phase
    if [ -f "$TRAINING_LOG" ]; then
        TAIL_OUTPUT=$(tail -20 "$TRAINING_LOG" 2>/dev/null)

        # Check for checkpoint save (highest priority)
        if echo "$TAIL_OUTPUT" | grep -q "Saving checkpoint"; then
            echo "checkpoint_save"
            return
        fi

        # Check for validation
        if echo "$TAIL_OUTPUT" | grep -qE "val/loss|Keyword argument.*enable_thinking.*ignored"; then
            echo "validation"
            return
        fi
    fi

    echo "training"
}

# Function to calculate memory growth rate
calculate_growth_rate() {
    local current_usage=$1
    local current_time=$2

    if [ "$PREV_CGROUP_USAGE" -ne 0 ]; then
        local mem_delta=$(python3 -c "print(f'{(${current_usage} - ${PREV_CGROUP_USAGE}) / 1024**3:.2f}')")
        local time_delta=$(python3 -c "print(f'{${current_time} - ${PREV_TIMESTAMP}:.1f}')")
        local growth_rate=$(python3 -c "print(f'{(${current_usage} - ${PREV_CGROUP_USAGE}) / 1024**3 / (${current_time} - ${PREV_TIMESTAMP}):.3f}') if ${time_delta} > 0 else 0" 2>/dev/null || echo "0")

        echo "${growth_rate}"
    else
        echo "0"
    fi
}

while true; do
    CURRENT_TIME=$(date +%s.%N)
    PHASE=$(detect_phase)

    # Adaptive interval based on phase
    case "$PHASE" in
        checkpoint_save)
            INTERVAL=$CHECKPOINT_INTERVAL
            ;;
        validation)
            INTERVAL=$VALIDATION_INTERVAL
            ;;
        *)
            INTERVAL=$BASE_INTERVAL
            ;;
    esac

    {
        echo ""
        echo "--- $(date -u '+%Y-%m-%d %H:%M:%S UTC') [PHASE: $PHASE] ---"

        # Cgroup memory (what k8s sees)
        CGROUP_USAGE=$(cat /sys/fs/cgroup/memory.current 2>/dev/null || echo 0)
        CGROUP_LIMIT=$(cat /sys/fs/cgroup/memory.max 2>/dev/null || echo 0)
        CGROUP_GB=$(python3 -c "print(f'{${CGROUP_USAGE} / 1024**3:.1f}')")
        CGROUP_LIMIT_GB=$(python3 -c "print(f'{${CGROUP_LIMIT} / 1024**3:.0f}')")
        CGROUP_PCT=$(python3 -c "print(f'{${CGROUP_USAGE} / ${CGROUP_LIMIT} * 100:.1f}')")

        # Calculate memory growth rate
        GROWTH_RATE=$(calculate_growth_rate "$CGROUP_USAGE" "$CURRENT_TIME")

        echo "CGROUP_MEM: ${CGROUP_GB} / ${CGROUP_LIMIT_GB} GB (${CGROUP_PCT}%)"
        echo "MEM_GROWTH: ${GROWTH_RATE} GB/s"

        # System memory
        free -h | head -2

        # Count python3 processes and their total RSS
        NPROCS=$(pgrep -c python3 2>/dev/null || echo 0)
        TOTAL_RSS=$(ps -eo rss,comm | grep python3 | awk '{sum+=$1} END {printf "%.1f", sum/1024/1024}')
        echo "PYTHON3_PROCS: ${NPROCS}, TOTAL_RSS: ${TOTAL_RSS} GB"

        # Top 5 python3 processes by memory (only if phase is critical)
        if [ "$PHASE" = "checkpoint_save" ] || [ "$PHASE" = "validation" ]; then
            echo "TOP_PROCS:"
            ps -eo pid,rss,vsz,comm --sort=-rss | grep python3 | head -5 | awk '{printf "  PID=%s RSS=%.1fGB VSZ=%.1fGB\n", $1, $2/1024/1024, $3/1024/1024}'
        fi

        # GPU memory
        nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits 2>/dev/null | while IFS=', ' read -r idx used total util; do
            echo "GPU${idx}: ${used}/${total} MiB (${util}% util)"
        done

        # Alert on rapid memory growth (> 10 GB/s is abnormal)
        if python3 -c "exit(0 if float('${GROWTH_RATE}') > 10.0 else 1)" 2>/dev/null; then
            echo "⚠️  ALERT: Rapid memory growth detected! ${GROWTH_RATE} GB/s"
        fi

        # Warning at multiple thresholds
        if python3 -c "exit(0 if ${CGROUP_USAGE} / ${CGROUP_LIMIT} > 0.95 else 1)" 2>/dev/null; then
            echo "🔴 CRITICAL: Memory usage above 95% of pod limit! OOM IMMINENT!"
        elif python3 -c "exit(0 if ${CGROUP_USAGE} / ${CGROUP_LIMIT} > 0.90 else 1)" 2>/dev/null; then
            echo "🟠 CRITICAL: Memory usage above 90% of pod limit! OOM risk high!"
        elif python3 -c "exit(0 if ${CGROUP_USAGE} / ${CGROUP_LIMIT} > 0.80 else 1)" 2>/dev/null; then
            echo "🟡 WARNING: Memory usage above 80% of pod limit!"
        elif python3 -c "exit(0 if ${CGROUP_USAGE} / ${CGROUP_LIMIT} > 0.70 else 1)" 2>/dev/null; then
            echo "ℹ️  NOTICE: Memory usage above 70% of pod limit."
        fi

        # Extra warning during checkpoint save in high memory state
        if [ "$PHASE" = "checkpoint_save" ]; then
            if python3 -c "exit(0 if ${CGROUP_USAGE} / ${CGROUP_LIMIT} > 0.75 else 1)" 2>/dev/null; then
                echo "⚠️  CHECKPOINT SAVE IN HIGH MEMORY STATE - monitoring closely"
            fi
        fi

    } >> "$LOGFILE" 2>&1

    # Update previous values for growth rate calculation
    PREV_CGROUP_USAGE=$CGROUP_USAGE
    PREV_TIMESTAMP=$CURRENT_TIME

    sleep "$INTERVAL"
done

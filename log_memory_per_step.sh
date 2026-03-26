#!/usr/bin/env bash
# Per-step CPU memory logger for detecting memory leaks
# Monitors training log and records CPU memory after each completed step
# Usage: bash log_memory_per_step.sh [training_log] [output_csv]
#
# Run alongside training:
#   bash log_memory_per_step.sh training_v3.log memory_per_step.csv &

TRAINING_LOG=${1:-/fsx-shared/juncheng/EHR/training_v3.log}
OUTPUT_CSV=${2:-/fsx-shared/juncheng/EHR/memory_per_step.csv}

# Initialize CSV with header
echo "timestamp,step,cgroup_mem_gb,cgroup_limit_gb,cgroup_pct,python_procs,total_rss_gb,free_mem_gb" > "$OUTPUT_CSV"

echo "=== Per-Step Memory Logger ==="
echo "Training log: $TRAINING_LOG"
echo "Output CSV: $OUTPUT_CSV"
echo "Monitoring for step completions..."
echo "========================================"

# Track last processed step to avoid duplicates
LAST_STEP=-1

# Follow training log in real-time
tail -F "$TRAINING_LOG" 2>/dev/null | while read -r line; do
    # Detect step completion: "step:N - train/loss:..."
    if echo "$line" | grep -qE "^step:[0-9]+ - train/loss:"; then
        # Extract step number
        STEP=$(echo "$line" | sed -n 's/^step:\([0-9]*\).*/\1/p')

        # Skip if already processed
        if [ "$STEP" -le "$LAST_STEP" ]; then
            continue
        fi

        LAST_STEP=$STEP

        # Wait 1 second for memory to stabilize after step completion
        sleep 1

        # Collect memory metrics
        TIMESTAMP=$(date -u '+%Y-%m-%d %H:%M:%S')

        # Cgroup memory
        CGROUP_USAGE=$(cat /sys/fs/cgroup/memory.current 2>/dev/null || echo 0)
        CGROUP_LIMIT=$(cat /sys/fs/cgroup/memory.max 2>/dev/null || echo 0)
        CGROUP_GB=$(python3 -c "print(f'{${CGROUP_USAGE} / 1024**3:.2f}')" 2>/dev/null || echo "0")
        CGROUP_LIMIT_GB=$(python3 -c "print(f'{${CGROUP_LIMIT} / 1024**3:.0f}')" 2>/dev/null || echo "0")
        CGROUP_PCT=$(python3 -c "print(f'{${CGROUP_USAGE} / ${CGROUP_LIMIT} * 100:.2f}')" 2>/dev/null || echo "0")

        # Python processes
        NPROCS=$(pgrep -c python3 2>/dev/null || echo 0)
        TOTAL_RSS=$(ps -eo rss,comm | grep python3 | awk '{sum+=$1} END {printf "%.2f", sum/1024/1024}' 2>/dev/null || echo "0")

        # System free memory
        FREE_MEM=$(free -g | awk 'NR==2 {print $4}')

        # Write to CSV
        echo "${TIMESTAMP},${STEP},${CGROUP_GB},${CGROUP_LIMIT_GB},${CGROUP_PCT},${NPROCS},${TOTAL_RSS},${FREE_MEM}" >> "$OUTPUT_CSV"

        # Console output
        echo "[$(date -u '+%H:%M:%S')] Step ${STEP}: ${CGROUP_GB}/${CGROUP_LIMIT_GB} GB (${CGROUP_PCT}%) | RSS: ${TOTAL_RSS} GB"

        # Alert if memory increasing trend detected (every 10 steps)
        if [ $((STEP % 10)) -eq 0 ]; then
            # Calculate average memory for last 10 steps vs previous 10 steps
            RECENT_AVG=$(tail -10 "$OUTPUT_CSV" | awk -F',' 'NR>1 {sum+=$3; count++} END {if(count>0) printf "%.2f", sum/count; else print "0"}')
            PREV_AVG=$(tail -20 "$OUTPUT_CSV" | head -10 | awk -F',' 'NR>1 {sum+=$3; count++} END {if(count>0) printf "%.2f", sum/count; else print "0"}')

            if [ $(python3 -c "print(1 if float('${RECENT_AVG}') > float('${PREV_AVG}') + 10 else 0)" 2>/dev/null || echo 0) -eq 1 ]; then
                DELTA=$(python3 -c "print(f'{float(\"${RECENT_AVG}\") - float(\"${PREV_AVG}\"):.2f}')" 2>/dev/null || echo "0")
                echo "⚠️  WARNING: Memory increasing trend detected! Last 10 steps avg: ${RECENT_AVG} GB vs prev 10: ${PREV_AVG} GB (delta: +${DELTA} GB)"
            fi
        fi

        # Critical alert at high memory
        if python3 -c "exit(0 if ${CGROUP_USAGE} / ${CGROUP_LIMIT} > 0.85 else 1)" 2>/dev/null; then
            echo "🔴 CRITICAL: Memory at ${CGROUP_PCT}% after step ${STEP}!"
        fi
    fi
done

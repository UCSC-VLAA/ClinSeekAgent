#!/usr/bin/env python3
"""
Analyze per-step memory log to detect memory leaks
Usage: python analyze_memory_leak.py memory_per_step.csv
"""

import sys
import pandas as pd
import numpy as np
from pathlib import Path

def analyze_memory_leak(csv_path):
    """Analyze memory trend from per-step log"""

    if not Path(csv_path).exists():
        print(f"Error: {csv_path} not found")
        return

    # Load data
    df = pd.read_csv(csv_path)

    if len(df) < 10:
        print(f"Not enough data points ({len(df)} steps). Need at least 10 steps for analysis.")
        return

    print("=" * 80)
    print(f"Memory Leak Analysis: {csv_path}")
    print("=" * 80)
    print(f"Total steps logged: {len(df)}")
    print(f"Step range: {df['step'].min()} → {df['step'].max()}")
    print()

    # Basic statistics
    print("Memory Statistics:")
    print(f"  Initial memory (step {df.iloc[0]['step']}): {df.iloc[0]['cgroup_mem_gb']:.2f} GB")
    print(f"  Final memory (step {df.iloc[-1]['step']}): {df.iloc[-1]['cgroup_mem_gb']:.2f} GB")
    print(f"  Delta: {df.iloc[-1]['cgroup_mem_gb'] - df.iloc[0]['cgroup_mem_gb']:+.2f} GB")
    print(f"  Min memory: {df['cgroup_mem_gb'].min():.2f} GB")
    print(f"  Max memory: {df['cgroup_mem_gb'].max():.2f} GB")
    print(f"  Mean memory: {df['cgroup_mem_gb'].mean():.2f} GB")
    print(f"  Std dev: {df['cgroup_mem_gb'].std():.2f} GB")
    print()

    # Linear regression to detect trend
    from scipy import stats

    slope, intercept, r_value, p_value, std_err = stats.linregress(df['step'], df['cgroup_mem_gb'])

    print("Trend Analysis (Linear Regression):")
    print(f"  Slope: {slope:.4f} GB/step")
    print(f"  R²: {r_value**2:.4f} (1.0 = perfect linear fit)")
    print(f"  P-value: {p_value:.6f} (< 0.05 = statistically significant)")
    print()

    # Memory leak detection
    if p_value < 0.05 and slope > 0.01:
        print("🔴 MEMORY LEAK DETECTED!")
        print(f"   Memory is increasing at {slope:.4f} GB per step")

        # Project when OOM will occur
        limit_gb = df['cgroup_limit_gb'].iloc[0]
        current_gb = df.iloc[-1]['cgroup_mem_gb']
        remaining_gb = limit_gb - current_gb

        if slope > 0:
            steps_until_oom = remaining_gb / slope
            print(f"   Current memory: {current_gb:.2f} GB / {limit_gb:.0f} GB")
            print(f"   Projected OOM at step: {df.iloc[-1]['step'] + int(steps_until_oom)}")
        print()

        # Possible causes
        print("Possible causes:")
        print("  1. Gradient accumulation without proper clearing")
        print("  2. Memory not freed after optimizer step")
        print("  3. Cached tensors in model buffers")
        print("  4. Memory fragmentation accumulation")
        print("  5. torch.compile caching graphs indefinitely")
        print()

        print("Recommended actions:")
        print("  1. Add explicit memory cleanup: torch.cuda.empty_cache() after validation")
        print("  2. Check for tensor accumulation in training loop")
        print("  3. Reduce torch.compile cache: TORCH_COMPILE_CACHE_SIZE=1")
        print("  4. Monitor RSS growth: check if Python process memory grows")
        print()

    elif slope > 0.001:
        print("⚠️  SLOW MEMORY GROWTH")
        print(f"   Memory is slowly increasing at {slope:.4f} GB per step")
        print("   This may be normal (e.g., dataloader caching) but monitor closely.")
        print()
    else:
        print("✅ NO MEMORY LEAK DETECTED")
        print("   Memory usage is stable over time.")
        print()

    # Check for periodic spikes (validation/checkpoint)
    print("Memory Variation Analysis:")
    rolling_std = df['cgroup_mem_gb'].rolling(window=10, min_periods=5).std()
    if rolling_std.max() > 10:
        print(f"  High variance detected (std up to {rolling_std.max():.2f} GB)")
        print("  This is normal for validation/checkpoint phases.")
    else:
        print(f"  Low variance (std < 10 GB) - memory usage is consistent")
    print()

    # Top 5 highest memory steps
    print("Top 5 Highest Memory Steps:")
    top5 = df.nlargest(5, 'cgroup_mem_gb')[['step', 'cgroup_mem_gb', 'cgroup_pct']]
    for _, row in top5.iterrows():
        print(f"  Step {row['step']:3d}: {row['cgroup_mem_gb']:.2f} GB ({row['cgroup_pct']:.1f}%)")
    print()

    # Summary
    print("=" * 80)
    if p_value < 0.05 and slope > 0.01:
        print("CONCLUSION: Memory leak detected. Training will likely OOM before completion.")
        print("            Investigate and fix before relaunching.")
    elif slope > 0.001:
        print("CONCLUSION: Slow memory growth observed. Monitor closely during training.")
    else:
        print("CONCLUSION: Memory usage is stable. No leak detected.")
    print("=" * 80)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python analyze_memory_leak.py memory_per_step.csv")
        sys.exit(1)

    analyze_memory_leak(sys.argv[1])

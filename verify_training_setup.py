#!/usr/bin/env python3
"""
Verify training setup before launching full training.
Checks: data files, model access, GPU availability, dependencies.
"""

import os
import sys

def check_data_files():
    """Check if dataset files exist."""
    print("=" * 60)
    print("Checking dataset files...")
    train_file = os.path.expanduser('~/data/deepmed_trajectory/train.parquet')
    val_file = os.path.expanduser('~/data/deepmed_trajectory/val.parquet')

    if os.path.exists(train_file):
        size_mb = os.path.getsize(train_file) / (1024 * 1024)
        print(f"  ✓ Train file exists: {train_file} ({size_mb:.2f} MB)")
    else:
        print(f"  ✗ Train file missing: {train_file}")
        return False

    if os.path.exists(val_file):
        size_mb = os.path.getsize(val_file) / (1024 * 1024)
        print(f"  ✓ Val file exists: {val_file} ({size_mb:.2f} MB)")
    else:
        print(f"  ✗ Val file missing: {val_file}")
        return False

    # Load and check data format
    try:
        import pandas as pd
        df = pd.read_parquet(train_file)
        print(f"  ✓ Train samples: {len(df)}")
        print(f"  ✓ Columns: {df.columns.tolist()}")
        if 'messages' in df.columns:
            print(f"  ✓ Messages column exists")
            sample_msg = df['messages'].iloc[0]
            print(f"  ✓ First message has {len(sample_msg)} turns")
        else:
            print(f"  ✗ Messages column not found")
            return False
    except Exception as e:
        print(f"  ✗ Error loading parquet: {e}")
        return False

    return True

def check_dependencies():
    """Check if all required packages are installed."""
    print("\n" + "=" * 60)
    print("Checking dependencies...")

    packages = {
        'torch': 'PyTorch',
        'transformers': 'Transformers',
        'verl': 'verl',
        'ray': 'Ray',
        'vllm': 'vLLM',
        'wandb': 'Weights & Biases'
    }

    all_ok = True
    for pkg, name in packages.items():
        try:
            mod = __import__(pkg)
            version = getattr(mod, '__version__', 'unknown')
            print(f"  ✓ {name}: {version}")
        except ImportError:
            print(f"  ✗ {name}: NOT INSTALLED")
            all_ok = False

    # Check flash-attn separately (optional)
    try:
        import flash_attn
        print(f"  ✓ flash-attn: {flash_attn.__version__} (optional)")
    except ImportError:
        print(f"  ⚠ flash-attn: NOT INSTALLED (will use SDPA attention)")

    return all_ok

def check_gpu():
    """Check GPU availability."""
    print("\n" + "=" * 60)
    print("Checking GPU availability...")

    try:
        import torch
        if torch.cuda.is_available():
            gpu_count = torch.cuda.device_count()
            print(f"  ✓ CUDA available: {torch.cuda.is_available()}")
            print(f"  ✓ GPU count: {gpu_count}")
            for i in range(gpu_count):
                name = torch.cuda.get_device_name(i)
                mem_gb = torch.cuda.get_device_properties(i).total_memory / (1024**3)
                print(f"    GPU {i}: {name} ({mem_gb:.1f} GB)")
            return True
        else:
            print(f"  ✗ CUDA not available")
            return False
    except Exception as e:
        print(f"  ✗ Error checking GPU: {e}")
        return False

def check_model_access():
    """Check if model is accessible (without downloading)."""
    print("\n" + "=" * 60)
    print("Checking model access...")

    try:
        from transformers import AutoTokenizer
        model_name = "Qwen/Qwen3.5-27B"
        print(f"  Checking: {model_name}")

        # This will check if model exists without downloading weights
        from huggingface_hub import model_info
        info = model_info(model_name)
        print(f"  ✓ Model accessible: {info.modelId}")
        print(f"  ✓ Model size: {info.siblings and sum(s.size for s in info.siblings if s.size) / (1024**3):.1f} GB")
        return True
    except Exception as e:
        print(f"  ✗ Error checking model: {e}")
        print(f"  Note: You may need to login: huggingface-cli login")
        return False

def main():
    print("\nDeepMed Training Setup Verification")
    print("=" * 60)

    results = {
        'Data files': check_data_files(),
        'Dependencies': check_dependencies(),
        'GPU': check_gpu(),
        'Model access': check_model_access()
    }

    print("\n" + "=" * 60)
    print("Summary:")
    for check, passed in results.items():
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {status}: {check}")

    print("=" * 60)

    if all(results.values()):
        print("\n🎉 All checks passed! Ready to start training.")
        print("\nTo start training with 4 GPUs:")
        print("  cd /fsx-shared/juncheng/EHR/verl")
        print("  bash examples/sft/deepmed/run_qwen3_5_27b_sft.sh 4 ./checkpoints/deepmed_qwen3.5_27b")
        return 0
    else:
        print("\n⚠️  Some checks failed. Please fix the issues before training.")
        return 1

if __name__ == "__main__":
    sys.exit(main())

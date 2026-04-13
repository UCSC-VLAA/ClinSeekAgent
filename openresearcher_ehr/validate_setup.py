#!/usr/bin/env python3
"""
Setup Validation Script for OpenResearcher + EHR Integration

Run this script to verify that all prerequisites are met before using the system.
"""
import sys
import os
import subprocess


def check_section(title):
    """Print a section header."""
    print(f"\n{'='*70}")
    print(f"  {title}")
    print('='*70)


def check_status(name, status, details=""):
    """Print check status."""
    symbol = "✅" if status else "❌"
    print(f"{symbol} {name}")
    if details:
        print(f"   → {details}")
    return status


def main():
    """Run all validation checks."""
    print("\n" + "="*70)
    print(" "*15 + "Setup Validation")
    print("="*70)

    all_passed = True

    # Check 1: File Structure
    check_section("File Structure")
    expected_files = [
        "data_utils.py",
        "ehr_pool.py",
        "deploy_agent.py",
        "browser.py",
        "README.md",
        "QUICKSTART.md",
        "test_integration.sh",
        "test_queries_ehr.jsonl",
        "test_queries_hybrid.jsonl",
        "test_queries_web.jsonl",
        "example_usage.py",
        "requirements.txt"
    ]

    missing_files = []
    for file in expected_files:
        if os.path.exists(file):
            check_status(f"File: {file}", True)
        else:
            check_status(f"File: {file}", False, "MISSING")
            missing_files.append(file)
            all_passed = False

    # Check 2: Python Dependencies
    check_section("Python Dependencies")

    deps_to_check = {
        "httpx": "httpx",
        "boto3": "boto3 (AWS SDK)",
        "botocore": "botocore (AWS SDK)",
    }

    for module, desc in deps_to_check.items():
        try:
            __import__(module)
            check_status(desc, True)
        except ImportError:
            check_status(desc, False, f"Install with: pip install {module}")
            all_passed = False

    # Check 3: AWS Configuration
    check_section("AWS Configuration")

    try:
        result = subprocess.run(
            ["aws", "sts", "get-caller-identity"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            import json
            identity = json.loads(result.stdout)
            check_status(
                "AWS Credentials",
                True,
                f"Account: {identity.get('Account', 'N/A')}"
            )
        else:
            check_status(
                "AWS Credentials",
                False,
                "Run: aws configure"
            )
            all_passed = False
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        check_status(
            "AWS Credentials",
            False,
            f"Error: {str(e)}"
        )
        all_passed = False

    # Check 4: EHR MCP Server
    check_section("EHR MCP Server")

    try:
        import httpx
        client = httpx.Client(timeout=3.0)
        response = client.get("http://127.0.0.1:5003/mcp/health")
        if response.status_code == 200:
            check_status("MCP Server Running", True, "http://127.0.0.1:5003/mcp")
        else:
            check_status(
                "MCP Server Running",
                False,
                f"HTTP {response.status_code}"
            )
            all_passed = False
    except Exception as e:
        check_status(
            "MCP Server Running",
            False,
            "Start with: cd ../; python src/run_mcp_server.py --mode http --port 5003 --data_path ../data/AgentEHR-Bench/MIMICIVAgentBench"
        )
        all_passed = False

    # Check 5: EHR Data
    check_section("EHR Data")

    ehr_data_path = "../data/AgentEHR-Bench/MIMICIVAgentBench"
    if os.path.exists(ehr_data_path):
        check_status(
            "EHR Data Directory",
            True,
            f"Path: {ehr_data_path}"
        )
        # Check for patient databases
        patient_db_path = os.path.join(ehr_data_path, "common/patient_db")
        if os.path.exists(patient_db_path):
            db_files = [f for f in os.listdir(patient_db_path) if f.endswith('.db')]
            if db_files:
                check_status(
                    "Patient Databases",
                    True,
                    f"Found {len(db_files)} patient DBs"
                )
            else:
                check_status("Patient Databases", False, "No .db files found")
                all_passed = False
        else:
            check_status(
                "Patient Databases",
                False,
                "patient_db directory not found"
            )
            all_passed = False
    else:
        check_status(
            "EHR Data Directory",
            False,
            f"Path not found: {ehr_data_path}"
        )
        all_passed = False

    # Check 6: OpenResearcher Dependencies
    check_section("OpenResearcher Dependencies (Optional)")

    openresearcher_path = "/fsx-shared/juncheng/OpenResearcher"
    if os.path.exists(openresearcher_path):
        check_status(
            "OpenResearcher Directory",
            True,
            openresearcher_path
        )

        # Check for Bedrock generator
        bedrock_gen_path = os.path.join(
            openresearcher_path,
            "utils/bedrock_generator.py"
        )
        if os.path.exists(bedrock_gen_path):
            check_status("Bedrock Generator", True)
        else:
            check_status("Bedrock Generator", False, "File not found")

        # Try importing
        sys.path.append(openresearcher_path)
        try:
            from utils.bedrock_generator import BedrockAsyncGenerator
            check_status("BedrockAsyncGenerator Import", True)
        except ImportError as e:
            check_status(
                "BedrockAsyncGenerator Import",
                False,
                f"Error: {str(e)}"
            )
    else:
        check_status(
            "OpenResearcher Directory",
            False,
            f"Path not found: {openresearcher_path}"
        )

    # Check 7: Environment Variables
    check_section("Environment Variables (Optional)")

    env_vars = {
        "AWS_REGION": "AWS region for Bedrock",
        "SERPER_API_KEY": "Serper API key for web search (optional)",
    }

    for var, desc in env_vars.items():
        value = os.getenv(var)
        if value:
            masked_value = value[:8] + "..." if len(value) > 8 else value
            check_status(f"{var}", True, f"Set: {masked_value}")
        else:
            check_status(f"{var}", False, f"Not set ({desc})")

    # Summary
    check_section("Summary")

    if all_passed:
        print("\n✅ All critical checks passed!")
        print("\nYou can now run:")
        print("  ./test_integration.sh")
        print("  python example_usage.py")
        print("  python deploy_agent.py --data_path test_queries_web.jsonl --output_dir ./results")
    else:
        print("\n❌ Some checks failed. Please fix the issues above.")
        print("\nCommon fixes:")
        print("  1. Install dependencies: pip install -r requirements.txt")
        print("  2. Configure AWS: aws configure")
        print("  3. Start MCP server: cd ../; python src/run_mcp_server.py --mode http --port 5003 --data_path ../data/AgentEHR-Bench/MIMICIVAgentBench")
        print("\nSee QUICKSTART.md for detailed setup instructions.")

    print("\n" + "="*70)
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())

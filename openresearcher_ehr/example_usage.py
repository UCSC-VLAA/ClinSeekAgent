"""
Example: Using OpenResearcher + EHR programmatically

This script demonstrates how to use the integrated system
in your own Python code.
"""
import asyncio
import sys
sys.path.append('/fsx-shared/juncheng/OpenResearcher')

from browser import LocalServiceBrowserBackend, SerperServiceBrowserBackend
from ehr_pool import EHRToolPool
from deploy_agent import BrowserPool, run_one_native
from utils.bedrock_generator import BedrockAsyncGenerator


async def example_web_only():
    """Example 1: Web search only (no EHR)"""
    print("\n" + "="*60)
    print("Example 1: Web Search Only")
    print("="*60)

    # Initialize generator
    generator = BedrockAsyncGenerator(
        model_id="us.anthropic.claude-sonnet-4-5-v1:0",
        region_name="us-west-2",
        max_tokens_default=8192
    )

    # Initialize browser pool (using Serper for simplicity)
    browser_pool = BrowserPool(
        search_url="http://localhost:8001",
        browser_backend="serper"  # or "local" if you have local backend
    )

    # Run query
    question = "What are the main symptoms of Type 2 diabetes?"
    messages = await run_one_native(
        question=question,
        qid="example_web_001",
        generator=generator,
        browser_pool=browser_pool,
        ehr_pool=None,  # No EHR tools
        max_rounds=20
    )

    # Extract final answer
    final_response = messages[-1]["content"]
    print(f"\nQuestion: {question}")
    print(f"Answer: {final_response[:500]}...")


async def example_ehr_only():
    """Example 2: EHR query only"""
    print("\n" + "="*60)
    print("Example 2: EHR Query Only")
    print("="*60)

    # Initialize generator
    generator = BedrockAsyncGenerator(
        model_id="us.anthropic.claude-sonnet-4-5-v1:0",
        region_name="us-west-2"
    )

    # Initialize pools
    browser_pool = BrowserPool(
        search_url="http://localhost:8001",
        browser_backend="serper"
    )

    ehr_pool = EHRToolPool(
        mcp_url="http://127.0.0.1:5002/mcp"
    )

    # Run query
    question = """Load patient 10000032's EHR at timestamp 2150-12-01 10:00:00.
    Then list all available tables and show me the columns in the prescriptions table."""

    messages = await run_one_native(
        question=question,
        qid="example_ehr_001",
        generator=generator,
        browser_pool=browser_pool,
        ehr_pool=ehr_pool,
        max_rounds=30
    )

    # Extract final answer
    final_response = messages[-1]["content"]
    print(f"\nQuestion: {question[:100]}...")
    print(f"Answer: {final_response[:500]}...")

    # Cleanup
    await ehr_pool.close()


async def example_hybrid():
    """Example 3: Hybrid query (web + EHR)"""
    print("\n" + "="*60)
    print("Example 3: Hybrid Query (Web + EHR)")
    print("="*60)

    # Initialize generator
    generator = BedrockAsyncGenerator(
        model_id="us.anthropic.claude-sonnet-4-5-v1:0",
        region_name="us-west-2"
    )

    # Initialize pools
    browser_pool = BrowserPool(
        search_url="http://localhost:8001",
        browser_backend="serper"
    )

    ehr_pool = EHRToolPool(
        mcp_url="http://127.0.0.1:5002/mcp"
    )

    # Run query
    question = """Search the web for typical HbA1c levels indicating diabetes.
    Then search PubMed for recent guidelines on diabetes diagnosis.
    Compare and summarize the consensus values."""

    messages = await run_one_native(
        question=question,
        qid="example_hybrid_001",
        generator=generator,
        browser_pool=browser_pool,
        ehr_pool=ehr_pool,
        max_rounds=50
    )

    # Extract final answer
    final_response = messages[-1]["content"]
    print(f"\nQuestion: {question[:100]}...")
    print(f"Answer: {final_response[:500]}...")

    # Cleanup
    await ehr_pool.close()


async def example_custom_workflow():
    """Example 4: Custom workflow with multiple patients"""
    print("\n" + "="*60)
    print("Example 4: Custom Workflow (Multiple Patients)")
    print("="*60)

    # Initialize generator
    generator = BedrockAsyncGenerator(
        model_id="us.anthropic.claude-sonnet-4-5-v1:0",
        region_name="us-west-2"
    )

    browser_pool = BrowserPool(
        search_url="http://localhost:8001",
        browser_backend="serper"
    )

    ehr_pool = EHRToolPool(
        mcp_url="http://127.0.0.1:5002/mcp"
    )

    # Process multiple patients
    patients = [
        ("10000032", "2150-12-01 10:00:00"),
        # Add more patients if available
    ]

    for subject_id, timestamp in patients:
        question = f"""
        Load patient {subject_id}'s EHR at timestamp {timestamp}.
        Identify any cardiac-related diagnoses using ICD code search.
        Then search PubMed for treatment guidelines for those conditions.
        """

        messages = await run_one_native(
            question=question,
            qid=f"workflow_{subject_id}",
            generator=generator,
            browser_pool=browser_pool,
            ehr_pool=ehr_pool,
            max_rounds=50
        )

        final_response = messages[-1]["content"]
        print(f"\nPatient {subject_id}:")
        print(f"Analysis: {final_response[:300]}...")
        print("-" * 60)

    await ehr_pool.close()


async def main():
    """Run all examples"""
    print("\n" + "="*70)
    print("OpenResearcher + EHR Integration - Usage Examples")
    print("="*70)

    try:
        # Choose which example to run
        print("\nSelect example to run:")
        print("1. Web search only")
        print("2. EHR query only")
        print("3. Hybrid (web + EHR)")
        print("4. Custom workflow")
        print("5. Run all")

        choice = input("\nEnter choice (1-5): ").strip()

        if choice == "1":
            await example_web_only()
        elif choice == "2":
            await example_ehr_only()
        elif choice == "3":
            await example_hybrid()
        elif choice == "4":
            await example_custom_workflow()
        elif choice == "5":
            await example_web_only()
            await example_ehr_only()
            await example_hybrid()
            await example_custom_workflow()
        else:
            print("Invalid choice. Running default example (web only)...")
            await example_web_only()

    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()

    print("\n" + "="*70)
    print("Examples completed!")
    print("="*70)


if __name__ == "__main__":
    # Prerequisites check
    print("Prerequisites:")
    print("1. AWS credentials configured (aws configure)")
    print("2. EHR MCP server running at http://127.0.0.1:5002/mcp")
    print("3. SERPER_API_KEY environment variable set (for web search)")
    print()

    response = input("Prerequisites met? (y/n): ").strip().lower()
    if response == 'y':
        asyncio.run(main())
    else:
        print("\nPlease complete prerequisites first:")
        print("  - aws configure")
        print("  - python /fsx-shared/juncheng/EHR/src/run_mcp_server.py --mode http --port 5002 ...")
        print("  - export SERPER_API_KEY=your_key")

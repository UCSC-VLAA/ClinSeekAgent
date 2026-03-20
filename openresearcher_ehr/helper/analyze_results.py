#!/usr/bin/env python3
"""
Analyze test results: tool usage, performance metrics, and trajectories.
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def analyze_results(results_file):
    """Analyze tool usage and performance from results JSONL file."""

    if not Path(results_file).exists():
        print(f"❌ Results file not found: {results_file}")
        return

    # Metrics
    total_queries = 0
    successful_queries = 0
    failed_queries = 0
    tool_usage = Counter()
    tool_sequence = defaultdict(list)
    total_rounds = []
    total_tokens = []
    errors = []

    print("\n" + "="*80)
    print(" "*25 + "RESULTS ANALYSIS")
    print("="*80)

    # Read results
    with open(results_file, 'r') as f:
        for line in f:
            if not line.strip():
                continue

            try:
                result = json.loads(line)
                total_queries += 1
                qid = result.get('qid', 'unknown')
                status = result.get('status', 'unknown')
                messages = result.get('messages', [])
                question = result.get('question', '')

                print(f"\n{'─'*80}")
                print(f"Query ID: {qid}")
                print(f"Question: {question[:100]}...")
                print(f"Status: {status}")

                if status == 'success':
                    successful_queries += 1
                else:
                    failed_queries += 1
                    if 'error' in result:
                        errors.append((qid, result['error']))

                # Count rounds and tool usage
                rounds = 0
                query_tools = []

                for msg in messages:
                    if msg.get('role') == 'assistant':
                        rounds += 1
                        tool_calls = msg.get('tool_calls', []) or []

                        for tc in tool_calls:
                            tool_name = tc.get('function', {}).get('name', 'unknown')
                            tool_usage[tool_name] += 1
                            query_tools.append(tool_name)

                total_rounds.append(rounds)
                tool_sequence[qid] = query_tools

                print(f"Rounds: {rounds}")
                print(f"Tools used: {len(query_tools)} calls")
                if query_tools:
                    print(f"Tool sequence: {' → '.join(query_tools[:10])}")
                    if len(query_tools) > 10:
                        print(f"  ... and {len(query_tools) - 10} more")

                # Show final answer
                if messages:
                    last_msg = messages[-1]
                    if last_msg.get('role') == 'assistant':
                        content = last_msg.get('content', '')
                        if content:
                            print(f"\nFinal Answer:")
                            print(f"  {content[:200]}...")

            except json.JSONDecodeError as e:
                print(f"⚠️  Failed to parse line: {e}")
                continue

    # Summary statistics
    print(f"\n{'='*80}")
    print(" "*25 + "SUMMARY STATISTICS")
    print("="*80)

    print(f"\n📊 Overall Performance:")
    print(f"  Total queries: {total_queries}")
    print(f"  Successful: {successful_queries} ({100*successful_queries/total_queries if total_queries > 0 else 0:.1f}%)")
    print(f"  Failed: {failed_queries} ({100*failed_queries/total_queries if total_queries > 0 else 0:.1f}%)")

    if total_rounds:
        print(f"\n🔄 Round Statistics:")
        print(f"  Average rounds: {sum(total_rounds)/len(total_rounds):.1f}")
        print(f"  Min rounds: {min(total_rounds)}")
        print(f"  Max rounds: {max(total_rounds)}")

    if tool_usage:
        print(f"\n🛠️  Tool Usage (Total: {sum(tool_usage.values())} calls):")
        for tool, count in sorted(tool_usage.items(), key=lambda x: x[1], reverse=True):
            percentage = 100 * count / sum(tool_usage.values())
            print(f"  {tool:40s}: {count:3d} calls ({percentage:5.1f}%)")

    if errors:
        print(f"\n❌ Errors ({len(errors)}):")
        for qid, error in errors[:5]:
            print(f"  [{qid}] {error[:100]}...")
        if len(errors) > 5:
            print(f"  ... and {len(errors) - 5} more errors")

    # Tool sequences analysis
    if tool_sequence:
        print(f"\n📝 Common Tool Patterns:")
        # Find most common 2-tool sequences
        bigrams = Counter()
        for tools in tool_sequence.values():
            for i in range(len(tools) - 1):
                bigrams[(tools[i], tools[i+1])] += 1

        if bigrams:
            print("  Most common tool sequences:")
            for (tool1, tool2), count in bigrams.most_common(5):
                print(f"    {tool1} → {tool2}: {count} times")

    print("\n" + "="*80)

    return {
        'total_queries': total_queries,
        'successful': successful_queries,
        'failed': failed_queries,
        'avg_rounds': sum(total_rounds)/len(total_rounds) if total_rounds else 0,
        'tool_usage': dict(tool_usage),
        'tool_sequences': {k: v for k, v in tool_sequence.items()},
    }


if __name__ == "__main__":
    # if len(sys.argv) < 2:
    #     print("Usage: python analyze_results.py <results.jsonl>")
    #     print("\nExample:")
    #     print("  python analyze_results.py ./test_results_opus/results.jsonl")
    #     sys.exit(1)

    # results_file = sys.argv[1]
    results_file = '/home/efs/zlt/deepresearch/openresearcher_ehr/diagnoses_ccs_500_serper_results_fixed_20260316_2125/results.jsonl'
    analyze_results(results_file)

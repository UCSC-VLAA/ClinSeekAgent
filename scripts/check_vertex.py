#!/usr/bin/env python3
"""Smallest possible Claude-on-Vertex call, through the ClinSeekAgent backend.

Run it to confirm credentials, project, region and model access before starting
a real eval. It makes one ~20-token request.

    set -a; . ./.env; set +a          # from the repository root
    python scripts/check_vertex.py

Pass a model ID to pin one instead of auto-selecting:

    ... scripts/check_vertex.py claude-sonnet-4-6
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "clinseekagent"))


async def main() -> int:
    try:
        from vertex_backend import VertexAsyncGenerator
    except ImportError as exc:
        print(f"FAIL: cannot import vertex_backend ({exc})")
        print("      Use the agent venv (venvs/requirements/bedrock_agent.txt)")
        return 1

    model = sys.argv[1] if len(sys.argv) > 1 else None

    try:
        gen = VertexAsyncGenerator(model_id=model)
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 1

    try:
        resp = await gen.chat_completion(
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=16,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAIL: {type(exc).__name__}: {exc}\n")
        text = str(exc).lower()
        if "401" in text or "unauthenticated" in text:
            print("→ Credentials not accepted. A Google API key (AIza...) cannot")
            print("  call Claude; point GOOGLE_APPLICATION_CREDENTIALS at a")
            print("  service-account JSON.")
        elif "403" in text or "permission" in text:
            print("→ Identity recognized but not authorized. Grant the service")
            print("  account roles/aiplatform.user on the project.")
        elif "404" in text or "not found" in text:
            print(f"→ {gen.model_id!r} is not served in {gen.region!r}. Try another")
            print("  model or region; availability varies per project.")
        elif "429" in text:
            print("→ Quota exhausted for this model. Request quota in the console.")
        return 1

    usage = resp.get("usage") or {}
    print(f"\nOK  model={gen.model_id}  region={gen.region}  project={gen.project_id}")
    print(f"    reply={resp['choices'][0]['message']['content'].strip()!r}")
    print(f"    usage={ {k: usage[k] for k in ('input_tokens', 'output_tokens') if k in usage} }")

    await gen.client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

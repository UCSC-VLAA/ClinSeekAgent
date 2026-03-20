#!/usr/bin/env python3
"""
Upload a local JSON file to the Hugging Face Hub as a private dataset.

Authentication is read from:
1. --token
2. HF_TOKEN
3. HUGGINGFACE_HUB_TOKEN
"""

import argparse
import os
import re
from pathlib import Path

os.environ['HF_TOKEN']='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'

DEFAULT_FILE = Path(
    "/home/efs/zlt/deepresearch/openresearcher_ehr/"
    "diagnoses_ccs_500_serper_results_fixed_20260316_2125/results_with_answers.json"
)


def load_hf_api():
    try:
        from huggingface_hub import HfApi
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing dependency: huggingface_hub\n"
            "Install it with: pip install huggingface_hub"
        ) from exc
    return HfApi()


def get_token(cli_token: str | None) -> str:
    token = (
        cli_token
        or os.environ.get("HF_TOKEN")
        or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    )
    if not token:
        raise SystemExit(
            "Missing Hugging Face token. Pass --token or set HF_TOKEN / "
            "HUGGINGFACE_HUB_TOKEN."
        )
    return token


def slugify_name(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-._")
    return slug.lower() or "dataset"


def resolve_repo_id(api, token: str, repo_id: str | None, file_path: Path) -> str:
    if repo_id and "/" in repo_id:
        return repo_id

    username = api.whoami(token=token)["name"]
    repo_name = repo_id or slugify_name(file_path.parent.name)
    return f"{username}/{repo_name}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload a local file to the Hugging Face Hub as a private dataset."
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=DEFAULT_FILE,
        help="Local file to upload.",
    )
    parser.add_argument(
        "--repo-id",
        help=(
            "Dataset repo id. You can pass either 'namespace/name' or just "
            "'name'. If omitted, defaults to your username plus the parent "
            "folder name."
        ),
        default='DeepMed_trajectory'
    )
    parser.add_argument(
        "--path-in-repo",
        help="Destination path inside the dataset repo. Defaults to the local filename.",
    )
    parser.add_argument(
        "--token",
        help="Hugging Face token. Prefer passing via HF_TOKEN env var instead.",
    )
    parser.add_argument(
        "--public",
        action="store_true",
        help="Create the dataset as public. Default is private.",
    )
    args = parser.parse_args()

    file_path = args.file
    if not file_path.exists():
        raise SystemExit(f"File not found: {file_path}")

    token = get_token(args.token)
    api = load_hf_api()
    repo_id = resolve_repo_id(api, token, args.repo_id, file_path)
    path_in_repo = args.path_in_repo or file_path.name
    private = not args.public

    api.create_repo(
        repo_id=repo_id,
        repo_type="dataset",
        private=private,
        exist_ok=True,
        token=token,
    )

    upload_url = api.upload_file(
        path_or_fileobj=str(file_path),
        path_in_repo=path_in_repo,
        repo_id=repo_id,
        repo_type="dataset",
        token=token,
    )

    visibility = "private" if private else "public"
    print(f"Dataset repo: {repo_id}")
    print(f"Visibility: {visibility}")
    print(f"Uploaded file: {file_path}")
    print(f"Path in repo: {path_in_repo}")
    print(f"Dataset URL: https://huggingface.co/datasets/{repo_id}")
    print(f"File URL: {upload_url}")


if __name__ == "__main__":
    main()

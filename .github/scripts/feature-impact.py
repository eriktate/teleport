#!/usr/bin/env python3
"""
Analyzes a pull request's description and changed files against the feature
matrix to determine which features are impacted by the change.

Outputs results as a GitHub Actions step summary and posts a PR comment.

Usage:
    python feature-impact.py \
        --matrix .github/feature-matrix.yaml \
        --pr-body <path-to-file-with-pr-body> \
        --changed-files <path-to-file-with-changed-files>
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error

try:
    import yaml
except ImportError:
    print("PyYAML not found, installing...", file=sys.stderr)
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyyaml", "-q"])
    import yaml


def load_matrix(path):
    with open(path) as f:
        return yaml.safe_load(f)


def normalize(text):
    """Lowercase and replace common separators with spaces for keyword matching."""
    return re.sub(r"[-_/]", " ", text.lower())


def path_matches(changed_file, relevant_path):
    """Return True if changed_file is under or equal to relevant_path."""
    # Normalize both paths – strip leading slashes for comparison
    cf = changed_file.strip("/")
    rp = relevant_path.strip("/")
    # Exact match or the changed file is inside the directory
    return cf == rp or cf.startswith(rp.rstrip("/") + "/")


def find_impacted_features(matrix, pr_body, changed_files):
    """
    Evaluate each feature against the PR body and changed files.

    Returns a list of dicts:
        {feature, matched_keywords, matched_paths}
    """
    results = []
    normalized_body = normalize(pr_body or "")

    for feature in matrix.get("features", []):
        feat_id = feature["id"]
        feat_name = feature["name"]
        keywords = [str(k) for k in feature.get("keywords", [])]
        relevant_paths = [str(p) for p in feature.get("relevant_paths", [])]

        # --- keyword matching against PR body ---
        matched_keywords = []
        for kw in keywords:
            pattern = r"\b" + re.escape(normalize(kw)) + r"\b"
            if re.search(pattern, normalized_body):
                matched_keywords.append(kw)

        # --- path matching against changed files ---
        matched_paths = []
        for changed in changed_files:
            for rp in relevant_paths:
                if path_matches(changed, rp):
                    matched_paths.append(changed)
                    break  # one match per changed file is enough

        if matched_keywords or matched_paths:
            results.append(
                {
                    "id": feat_id,
                    "name": feat_name,
                    "matched_keywords": matched_keywords,
                    "matched_paths": matched_paths,
                    "feature": feature,
                }
            )

    return results


def build_comment(impacted, matrix):
    """Build a Markdown comment body for the PR."""
    if not impacted:
        return (
            "## 🔍 Feature Impact Analysis\n\n"
            "No features from the feature matrix were detected as impacted by "
            "this pull request based on the PR description and changed files.\n\n"
            "_If this seems incorrect, consider adding relevant keywords to the "
            "PR description._"
        )

    # Build a lookup map for feature metadata
    feat_map = {f["id"]: f for f in matrix.get("features", [])}

    lines = ["## 🔍 Feature Impact Analysis", ""]
    lines.append(
        f"The following **{len(impacted)} feature(s)** appear to be impacted by "
        "this pull request:"
    )
    lines.append("")

    for item in impacted:
        feat = item["feature"]
        lines.append(f"### {feat['name']}")
        lines.append(f"**ID:** `{feat['id']}`")

        if item["matched_keywords"]:
            kw_list = ", ".join(f"`{k}`" for k in item["matched_keywords"])
            lines.append(f"**Matched keywords:** {kw_list}")

        if item["matched_paths"]:
            path_list = ", ".join(f"`{p}`" for p in item["matched_paths"])
            lines.append(f"**Matched paths:** {path_list}")

        # Relationships
        for rel_key, rel_label in [
            ("depends_on", "Depends on"),
            ("gated_by", "Gated by"),
            ("optionally_gated_by", "Optionally gated by"),
            ("emits", "Emits"),
            ("related_to", "Related to"),
        ]:
            refs = feat.get(rel_key, [])
            if refs:
                ref_names = []
                for ref_id in refs:
                    ref_feat = feat_map.get(ref_id)
                    ref_names.append(
                        f"`{ref_id}`" + (f" ({ref_feat['name']})" if ref_feat else "")
                    )
                lines.append(f"**{rel_label}:** {', '.join(ref_names)}")

        # Doc refs
        doc_refs = feat.get("doc_refs", [])
        if doc_refs:
            lines.append("**Docs:**")
            for ref in doc_refs:
                lines.append(f"  - {ref}")

        lines.append("")

    lines.append(
        "_This analysis is automated. Please review the impacted features and "
        "update tests / documentation accordingly._"
    )

    return "\n".join(lines)


def post_or_update_comment(token, repo, pr_number, body):
    """Post a new comment or update the existing bot comment on the PR."""
    marker = "## 🔍 Feature Impact Analysis"
    api_base = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }

    # List existing comments to find one to update
    existing_comment_id = None
    page = 1
    while True:
        url = f"{api_base}?per_page=100&page={page}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                comments = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            print(f"Error listing comments: {e}", file=sys.stderr)
            break

        for comment in comments:
            if (
                comment.get("user", {}).get("type") == "Bot"
                and marker in comment.get("body", "")
            ):
                existing_comment_id = comment["id"]
                break

        if existing_comment_id or len(comments) < 100:
            break
        page += 1

    payload = json.dumps({"body": body}).encode()

    if existing_comment_id:
        url = f"https://api.github.com/repos/{repo}/issues/comments/{existing_comment_id}"
        req = urllib.request.Request(url, data=payload, headers=headers, method="PATCH")
    else:
        req = urllib.request.Request(api_base, data=payload, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
            print(f"Comment {'updated' if existing_comment_id else 'posted'}: {result['html_url']}")
    except urllib.error.HTTPError as e:
        print(f"Error posting comment: {e}\n{e.read().decode()}", file=sys.stderr)
        sys.exit(1)


def write_step_summary(impacted, comment_body):
    """Write output to the GitHub Actions step summary file."""
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a") as f:
            f.write(comment_body + "\n")

    # Also emit a structured output for downstream steps
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        ids = ",".join(item["id"] for item in impacted)
        with open(output_file, "a") as f:
            f.write(f"impacted_features={ids}\n")
            f.write(f"impacted_count={len(impacted)}\n")


def main():
    parser = argparse.ArgumentParser(description="Detect impacted features for a PR.")
    parser.add_argument("--matrix", required=True, help="Path to feature-matrix.yaml")
    parser.add_argument(
        "--pr-body", required=True, help="Path to file containing the PR description"
    )
    parser.add_argument(
        "--changed-files",
        required=True,
        help="Path to file containing newline-separated list of changed files",
    )
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"), help="GitHub token")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), help="owner/repo")
    parser.add_argument("--pr-number", default=os.environ.get("PR_NUMBER"), help="PR number")
    args = parser.parse_args()

    matrix = load_matrix(args.matrix)

    with open(args.pr_body) as f:
        pr_body = f.read()

    with open(args.changed_files) as f:
        changed_files = [line.strip() for line in f if line.strip()]

    impacted = find_impacted_features(matrix, pr_body, changed_files)

    comment_body = build_comment(impacted, matrix)

    # Always write step summary
    write_step_summary(impacted, comment_body)

    # Post/update PR comment when running in CI
    if args.token and args.repo and args.pr_number:
        post_or_update_comment(args.token, args.repo, args.pr_number, comment_body)
    else:
        print("Skipping comment posting (missing token/repo/pr-number).")
        print("\n--- Feature Impact Report ---")
        print(comment_body)


if __name__ == "__main__":
    main()

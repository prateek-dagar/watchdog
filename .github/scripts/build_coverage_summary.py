import os
import glob
import json
import subprocess
import sys


def get_pr_number():
    """Extract PR number from the GitHub event payload JSON file."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not os.path.exists(event_path):
        return None
    try:
        with open(event_path) as f:
            event = json.load(f)
        return str(event.get("pull_request", {}).get("number", "") or event.get("number", ""))
    except Exception as e:
        print(f"Error reading event payload: {e}")
        return None


def get_artifacts_map(repo, run_id):
    """Fetch artifact name -> download URL mapping from the GitHub API."""
    artifacts_map = {}
    if not (repo and run_id):
        return artifacts_map
    try:
        output = subprocess.check_output([
            "gh", "api", f"repos/{repo}/actions/runs/{run_id}/artifacts",
            "-q", ".artifacts[] | {name: .name, id: .id}"
        ]).decode().strip()
        if output:
            for line in output.split("\n"):
                if not line.strip():
                    continue
                item = json.loads(line)
                artifacts_map[item["name"]] = (
                    f"https://github.com/{repo}/actions/runs/{run_id}/artifacts/{item['id']}"
                )
    except Exception as e:
        print(f"Error fetching artifacts from GitHub API: {e}")
    return artifacts_map


def get_failed_jobs_map(repo, run_id):
    """Fetch job name -> log URL for jobs that did not succeed."""
    jobs_map = {}
    if not (repo and run_id):
        return jobs_map
    try:
        output = subprocess.check_output([
            "gh", "api", f"repos/{repo}/actions/runs/{run_id}/jobs",
            "-q", '.jobs[] | {name: .name, id: .id, conclusion: .conclusion}'
        ]).decode().strip()
        if output:
            for line in output.split("\n"):
                if not line.strip():
                    continue
                job = json.loads(line)
                if job.get("conclusion") in ("failure", "cancelled", None):
                    jobs_map[job["name"]] = (
                        f"https://github.com/{repo}/actions/runs/{run_id}/job/{job['id']}"
                    )
    except Exception as e:
        print(f"Error fetching jobs from GitHub API: {e}")
    return jobs_map


def main():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")

    artifacts_map = get_artifacts_map(repo, run_id)
    failed_jobs_map = get_failed_jobs_map(repo, run_id)

    lines = []
    for path in glob.glob("coverage_pcts/coverage_pct_*"):
        folder_name = os.path.basename(path)
        # folder_name format: coverage_pct_<os>_<python_version>
        parts = folder_name.split("_")
        if len(parts) < 4:
            continue
        os_name = parts[2]
        py_ver = parts[3]

        filepath = os.path.join(path, "coverage_percentage.txt")
        if not os.path.exists(filepath):
            continue

        with open(filepath) as f:
            val = f.read().strip()

        os_emoji = "🐧"
        if "macos" in os_name:
            os_emoji = "🍎"
        elif "windows" in os_name:
            os_emoji = "🪟"

        # Build the coverage column — link to failed logs if tests failed
        # Determine failure either from the coverage file marker or from a failed job in the matrix
        job_failed = any(os_name in name and py_ver in name for name in failed_jobs_map)
        is_failed = val.startswith("❌") or job_failed
        if is_failed:
            # Prefer a link to the failed job log if we can find it
            job_url = None
            if job_failed:
                for job_name, url in failed_jobs_map.items():
                    if os_name in job_name and py_ver in job_name:
                        job_url = url
                        break
            coverage_md = f"[{val}]({job_url})" if job_url else val
        else:
            coverage_md = val

        # Build the HTML report column
        html_art_name = f"coverage_html_{os_name}_{py_ver}"
        report_link = artifacts_map.get(html_art_name, "")
        if report_link and not is_failed:
            report_md = f"[📥 Download]({report_link})"
        else:
            report_md = "—"

        lines.append((os_name, py_ver, f"| {os_emoji} {os_name} | {py_ver} | {coverage_md} | {report_md} |"))

    if not lines:
        print("No coverage data found.")
        sys.exit(1)

    # Sort results for a consistent table output
    lines.sort()

    summary_content = "## 📊 Test Coverage Summary\n\n"
    summary_content += "| OS | Python | Coverage | Report |\n"
    summary_content += "| :--- | :--- | :--- | :--- |\n"
    for _, _, line in lines:
        summary_content += line + "\n"

    # Write to GitHub step summary
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "summary.md")
    with open(summary_path, "a") as f:
        f.write(summary_content)

    # If any matrix entry failed, fail the step so the workflow is marked failed
    any_failed = any(val.startswith("❌") or any(os_name in name and py_ver in name for name in failed_jobs_map) for os_name, py_ver, _ in lines)
    if any_failed:
        sys.exit(1)

    # Post or update comment on Pull Request
    if os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
        pr_num = get_pr_number()
        if not pr_num:
            print("Could not determine PR number, skipping comment.")
            return

        try:
            comments = subprocess.check_output([
                "gh", "pr", "view", pr_num, "--json", "comments", "-q",
                '.comments[] | select(.body | contains("Test Coverage Summary")) | .id'
            ]).decode().strip().split("\n")
            comment_id = comments[0].strip() if comments and comments[0].strip() else ""
        except Exception as e:
            print(f"Error querying PR comments: {e}")
            comment_id = ""

        if comment_id:
            try:
                subprocess.run([
                    "gh", "api", "-X", "PATCH", f"repos/{repo}/issues/comments/{comment_id}",
                    "-F", f"body=@{summary_path}"
                ], check=True)
                print("Updated existing PR coverage comment.")
            except Exception as e:
                print(f"Error updating PR comment: {e}")
                sys.exit(1)
        else:
            try:
                subprocess.run([
                    "gh", "pr", "comment", pr_num, "--body-file", summary_path
                ], check=True)
                print("Created new PR coverage comment.")
            except Exception as e:
                print(f"Error creating PR comment: {e}")
                sys.exit(1)


if __name__ == "__main__":
    main()

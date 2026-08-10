import os
import glob
import json
import subprocess


def get_artifacts_map(repo, run_id):
    """Fetch artifact name → download URL mapping from the GitHub API."""
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
    """Fetch job name → log URL for jobs that did not succeed."""
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
        is_failed = val.startswith("❌")
        if is_failed:
            # Try to find the matching failed job log URL
            # Job names typically look like: "tox (ubuntu-latest, 3.13)"
            job_key = None
            for job_name, job_url in failed_jobs_map.items():
                if os_name in job_name and py_ver in job_name:
                    job_key = job_url
                    break
            if job_key:
                coverage_md = f"[{val}]({job_key})"
            else:
                coverage_md = val
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

    # Post or update comment on Pull Request
    if os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
        pr_num = os.environ.get("GITHUB_EVENT_NUMBER")

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
        else:
            try:
                subprocess.run([
                    "gh", "pr", "comment", pr_num, "--body-file", summary_path
                ], check=True)
                print("Created new PR coverage comment.")
            except Exception as e:
                print(f"Error creating PR comment: {e}")


if __name__ == "__main__":
    main()

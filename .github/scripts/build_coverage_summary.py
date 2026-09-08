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
            "gh", "api", "--paginate", f"repos/{repo}/actions/runs/{run_id}/artifacts",
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
            "gh", "api", "--paginate", f"repos/{repo}/actions/runs/{run_id}/jobs",
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


def get_pr_base_branch():
    """Extract PR base branch name from the GitHub event payload JSON file."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not os.path.exists(event_path):
        return "master"
    try:
        with open(event_path) as f:
            event = json.load(f)
        return event.get("pull_request", {}).get("base", {}).get("ref", "master")
    except Exception as e:
        print(f"Error reading event payload for base branch: {e}")
        return "master"


def get_latest_success_run_id(repo, branch):
    """Find the latest successful run ID on the base branch."""
    if not (repo and branch):
        return None
    try:
        output = subprocess.check_output([
            "gh", "api", f"repos/{repo}/actions/runs?branch={branch}&status=success&event=push&per_page=1",
            "-q", ".workflow_runs[0].id"
        ]).decode().strip()
        if output and output.isdigit():
            return int(output)
    except Exception as e:
        print(f"Error fetching latest success run for branch {branch}: {e}")
    return None


def get_pr_files(repo, pr_num):
    """Get mapping of filename -> status ('added', 'modified', etc.) in the PR."""
    files = {}
    if not (repo and pr_num):
        return files
    try:
        output = subprocess.check_output([
            "gh", "api", f"repos/{repo}/pulls/{pr_num}/files",
            "-q", '.[] | {filename: .filename, status: .status}'
        ]).decode().strip()
        if output:
            for line in output.split("\n"):
                if not line.strip():
                    continue
                item = json.loads(line)
                files[item["filename"]] = item.get("status", "modified")
    except Exception as e:
        print(f"Error fetching PR files: {e}")
    return files


def get_commit_sha():
    """Get the head commit SHA for the pull request or push."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not os.path.exists(event_path):
        return os.environ.get("GITHUB_SHA")
    try:
        with open(event_path) as f:
            event = json.load(f)
        return event.get("pull_request", {}).get("head", {}).get("sha") or os.environ.get("GITHUB_SHA")
    except Exception as e:
        print(f"Error getting commit SHA: {e}")
        return os.environ.get("GITHUB_SHA")


def set_commit_status(repo, sha, state, description, target_url=None):
    """Set GitHub commit status check."""
    if not (repo and sha):
        return
    try:
        cmd = [
            "gh", "api", f"repos/{repo}/statuses/{sha}",
            "-f", f"state={state}",
            "-f", "context=coverage/summary",
            "-f", f"description={description}"
        ]
        if target_url:
            cmd.extend(["-f", f"target_url={target_url}"])
        subprocess.run(cmd, check=True)
        print(f"Set commit status to {state}: {description}")
    except Exception as e:
        print(f"Error setting commit status: {e}")


def get_file_coverage_from_json(json_path):
    """Extract file -> percent_covered from a coverage.json file."""
    if not os.path.exists(json_path):
        return {}
    try:
        with open(json_path) as f:
            data = json.load(f)
        return {
            filepath: float(f_info.get("summary", {}).get("percent_covered", 0))
            for filepath, f_info in data.get("files", {}).items()
        }
    except Exception as e:
        print(f"Error reading coverage JSON {json_path}: {e}")
        return {}


def get_base_coverage_details(repo, run_id, os_name, py_ver):
    """Download base branch's artifact and read both pct and json file coverage."""
    pct = None
    file_cov = {}
    if not (repo and run_id):
        return pct, file_cov
    art_name = f"coverage_pct_{os_name}_{py_ver}"
    import tempfile
    import shutil
    temp_dir = tempfile.mkdtemp()
    try:
        subprocess.check_call([
            "gh", "run", "download", str(run_id), "-R", repo, "-n", art_name, "--dir", temp_dir
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pct_file = os.path.join(temp_dir, "coverage_percentage.txt")
        if os.path.exists(pct_file):
            with open(pct_file) as f:
                pct = f.read().strip()
        json_file = os.path.join(temp_dir, "coverage.json")
        if os.path.exists(json_file):
            file_cov = get_file_coverage_from_json(json_file)
    except Exception:
        pass
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return pct, file_cov


def parse_percentage(val_str):
    """Parse percentage string (like '85.42%') into float."""
    if not val_str or "%" not in val_str:
        return None
    try:
        return float(val_str.replace("%", "").strip())
    except ValueError:
        return None


def build_file_coverage_table(pr_files, current_file_cov, base_file_cov):
    """Build markdown table for modified files coverage."""
    rows = []
    for filepath in sorted(pr_files.keys()):
        status = pr_files[filepath]
        curr_pct = next((pct for k, pct in current_file_cov.items() if k.endswith(filepath) or filepath.endswith(k)), None)
        base_pct = next((pct for k, pct in base_file_cov.items() if k.endswith(filepath) or filepath.endswith(k)), None)
        if curr_pct is None:
            continue
        curr_str = f"{curr_pct:.2f}%"
        base_str = f"{base_pct:.2f}%" if base_pct is not None else "—"
        
        if status == "added":
            diff_str = "🟢 New File"
        elif base_pct is not None:
            diff = curr_pct - base_pct
            diff_str = f"🟢 +{diff:.2f}%" if diff > 0 else (f"🔴 {diff:.2f}%" if diff < 0 else "⚪ no change")
        else:
            diff_str = "—"
        rows.append(f"| `{filepath}` | {base_str} | {curr_str} | {diff_str} |")
    if not rows:
        return ""
    return "\n### 📁 Changed Files Coverage\n\n| File | Base | PR | Change |\n| :--- | :--- | :--- | :--- |\n" + "\n".join(rows) + "\n"


def main():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")

    artifacts_map = get_artifacts_map(repo, run_id)
    failed_jobs_map = get_failed_jobs_map(repo, run_id)

    base_branch = get_pr_base_branch()
    base_run_id = get_latest_success_run_id(repo, base_branch)

    lines = []
    primary_os, primary_py = "linux", "3.13"
    primary_val, primary_base_cov, primary_path = None, {}, None

    # Determine primary run path
    paths = glob.glob("coverage_pcts/coverage_pct_*")
    for path in paths:
        parts = os.path.basename(path).split("_")
        if len(parts) >= 4 and parts[2] == primary_os and parts[3] == primary_py:
            primary_path = path
            break
    if not primary_path and paths:
        primary_path = paths[0]
        parts = os.path.basename(primary_path).split("_")
        if len(parts) >= 4:
            primary_os, primary_py = parts[2], parts[3]

    for path in paths:
        parts = os.path.basename(path).split("_")
        if len(parts) < 4:
            continue
        os_name, py_ver = parts[2], parts[3]
        filepath = os.path.join(path, "coverage_percentage.txt")
        if not os.path.exists(filepath):
            continue

        with open(filepath) as f:
            val = f.read().strip()

        job_failed = any(os_name in name and py_ver in name for name in failed_jobs_map)
        is_failed = val.startswith("❌") or job_failed
        
        diff_str = ""
        base_file_cov = {}
        if base_run_id and not is_failed:
            base_val, base_file_cov = get_base_coverage_details(repo, base_run_id, os_name, py_ver)
            if base_val:
                curr_pct = parse_percentage(val)
                base_pct = parse_percentage(base_val)
                if curr_pct is not None and base_pct is not None:
                    diff = curr_pct - base_pct
                    diff_str = f" (🟢 +{diff:.2f}%)" if diff > 0 else (f" (🔴 {diff:.2f}%)" if diff < 0 else " (⚪ no change)")

        if path == primary_path:
            primary_val = val
            primary_base_cov = base_file_cov

        os_emoji = "🍎" if "macos" in os_name else ("🪟" if "windows" in os_name else "🐧")
        coverage_md = f"[{val}]({failed_jobs_map.get(next((n for n in failed_jobs_map if os_name in n and py_ver in n), ''), '')})" if is_failed else f"{val}{diff_str}"
        report_link = artifacts_map.get(f"coverage_html_{os_name}_{py_ver}", "")
        report_md = f"[📥 Download]({report_link})" if report_link and not is_failed else "—"

        lines.append((os_name, py_ver, f"| {os_emoji} {os_name} | {py_ver} | {coverage_md} | {report_md} |"))

    if not lines:
        sys.exit(1)

    lines.sort()
    summary_content = "## 📊 Test Coverage Summary\n\n| OS | Python | Coverage | Report |\n| :--- | :--- | :--- | :--- |\n" + "\n".join(line for _, _, line in lines) + "\n"

    # Changed files diff
    if event_name == "pull_request" and primary_path:
        pr_num = get_pr_number()
        curr_json = os.path.join(primary_path, "coverage.json")
        if pr_num and os.path.exists(curr_json):
            summary_content += build_file_coverage_table(get_pr_files(repo, pr_num), get_file_coverage_from_json(curr_json), primary_base_cov)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "summary.md")
    with open(summary_path, "a") as f:
        f.write(summary_content)

    # Set GitHub commit status
    if primary_val:
        status_state = "failure" if primary_val.startswith("❌") else "success"
        status_desc = "Tests failed" if status_state == "failure" else f"Coverage: {primary_val}"
        if status_state == "success" and base_run_id:
            # Add difference to status description
            curr_pct = parse_percentage(primary_val)
            base_val, _ = get_base_coverage_details(repo, base_run_id, primary_os, primary_py)
            base_pct = parse_percentage(base_val)
            if curr_pct is not None and base_pct is not None:
                diff = curr_pct - base_pct
                status_desc += f" (+{diff:.2f}%)" if diff > 0 else (f" ({diff:.2f}%)" if diff < 0 else " (no change)")
        set_commit_status(repo, get_commit_sha(), status_state, status_desc, f"https://github.com/{repo}/actions/runs/{run_id}")

    if any(val.startswith("❌") or any(os_name in name and py_ver in name for name in failed_jobs_map) for os_name, py_ver, _ in lines):
        sys.exit(1)

    if event_name == "pull_request":
        pr_num = get_pr_number()
        if not pr_num:
            return
        comment_id = ""
        try:
            output = subprocess.check_output(["gh", "api", f"repos/{repo}/issues/{pr_num}/comments", "-q", '.[] | select(.body | contains("Test Coverage Summary")) | .id']).decode().strip()
            if output:
                comment_id = output.split("\n")[0].strip()
        except Exception:
            pass

        comment_file = "pr_comment.md"
        with open(comment_file, "w") as f:
            f.write(summary_content)

        if comment_id:
            try:
                subprocess.run(["gh", "api", "-X", "PATCH", f"repos/{repo}/issues/comments/{comment_id}", "-F", f"body=@{comment_file}"], check=True)
            except Exception:
                sys.exit(1)
        else:
            try:
                subprocess.run(["gh", "pr", "comment", pr_num, "--body-file", comment_file], check=True)
            except Exception:
                sys.exit(1)


if __name__ == "__main__":
    main()

import os
import glob
import json
import subprocess

def main():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    
    # Query GitHub API to get artifact URLs for this run
    artifacts_map = {}
    if repo and run_id:
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
                    art_name = item["name"]
                    art_id = item["id"]
                    # Construct direct web download link for the artifact zip
                    artifacts_map[art_name] = f"https://github.com/{repo}/actions/runs/{run_id}/artifacts/{art_id}"
        except Exception as e:
            print(f"Error fetching artifacts from GitHub API: {e}")

    lines = []
    # Search for all coverage percentage files downloaded from artifacts
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
            
        # Check if we have a matching HTML report artifact
        html_art_name = f"coverage_html_{os_name}_{py_ver}"
        report_link = artifacts_map.get(html_art_name, "")
        report_md = f"[Download HTML]({report_link})" if report_link else "N/A"
            
        lines.append((os_name, py_ver, f"| {os_emoji} {os_name} | {py_ver} | {val} | {report_md} |"))

    # Sort results for a consistent table output
    lines.sort()
    
    summary_content = "## 📊 Test Coverage Summary\n\n"
    summary_content += "| OS | Python Version | Coverage | HTML Report |\n"
    summary_content += "| :--- | :--- | :--- | :--- |\n"
    for _, _, line in lines:
        summary_content += line + "\n"
        
    # Write to GitHub step summary if environment variable exists
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "summary.md")
    with open(summary_path, "a") as f:
        f.write(summary_content)

    # Post or update comment on Pull Request
    if os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
        pr_num = os.environ.get("GITHUB_EVENT_NUMBER")
        
        try:
            # Check for existing comment by GHA bot containing the summary header
            comments = subprocess.check_output([
                "gh", "pr", "view", pr_num, "--json", "comments", "-q",
                '.comments[] | select(.body | contains("Test Coverage Summary")) | .id'
            ]).decode().strip().split("\n")
            comment_id = comments[0].strip() if comments and comments[0].strip() else ""
        except Exception as e:
            print(f"Error querying PR comments: {e}")
            comment_id = ""
            
        if comment_id:
            # Update the existing comment
            try:
                subprocess.run([
                    "gh", "api", "-X", "PATCH", f"repos/{repo}/issues/comments/{comment_id}",
                    "-F", f"body=@{summary_path}"
                ], check=True)
                print("Updated existing PR coverage comment.")
            except Exception as e:
                print(f"Error updating PR comment: {e}")
        else:
            # Create a new comment
            try:
                subprocess.run([
                    "gh", "pr", "comment", pr_num, "--body-file", summary_path
                ], check=True)
                print("Created new PR coverage comment.")
            except Exception as e:
                print(f"Error creating PR comment: {e}")

if __name__ == "__main__":
    main()

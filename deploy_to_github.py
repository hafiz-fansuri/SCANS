#!/usr/bin/env python3
"""
One-shot GitHub deploy for SCANS.
Run from the SCANS project root:

    python deploy_to_github.py

You will be prompted (masked) for your GitHub PAT.
Set --public or --private to choose repo visibility.
"""
import getpass, json, subprocess, sys, os

def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0 and r.stderr:
        print(f"  STDERR: {r.stderr.strip()}", file=sys.stderr)
    return r

def main():
    token = getpass.getpass("GitHub PAT (repo scope, token not echoed): ").strip()
    if not token:
        print("No token entered. Aborting.")
        return

    visibility = "--public" if "--public" in sys.argv else "--private"
    repo_name = "SCANS"

    # 1. Create the GitHub repo via REST API
    print(f"\nCreating repository '{repo_name}' on GitHub ({visibility})...")
    import urllib.request
    api_url = "https://api.github.com/repos"
    payload = json.dumps({"name": repo_name, "private": visibility == "--private",
                          "description": "SCEP: Ship Conditions Environmental Prediction — vessel speed forecasting with ESP32 + ML"}).encode()
    req = urllib.request.Request(
        api_url, data=payload, method="POST",
        headers={"Authorization": f"token {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "SCANS-deploy-script"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            repo_data = json.loads(resp.read())
            print(f"  Created: {repo_data.get('html_url', repo_data.get('web_url', 'success'))}")
            clone_url = repo_data["clone_url"]
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  API error {e.code}: {body}", file=sys.stderr)
        return
    except Exception as e:
        print(f"  Error: {e}", file=sys.stderr)
        return

    # 2. Add remote & push
    remote_url = clone_url.replace("https://", f"https://oauth2:{token}@")
    print("\nAdding git remote 'origin'...")
    run(["git", "remote", "remove", "origin"], cwd=os.getcwd())
    run(["git", "remote", "add", "origin", remote_url], cwd=os.getcwd())

    print("Pushing to origin main...")
    r = run(["git", "push", "-u", "origin", "main"], cwd=os.getcwd())
    print(r.stdout)
    if r.stderr:
        print(r.stderr, file=sys.stderr)

    # 3. Clean up remote URL (don't leave token in config)
    run(["git", "remote", "set-url", "origin", clone_url], cwd=os.getcwd())
    print("\nDone! Token stripped from git config for security.")

if __name__ == "__main__":
    main()

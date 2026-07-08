#!/usr/bin/env python3
"""
FAWN Railway Deployment Monitor
Checks deployment status and provides detailed failure diagnostics
"""
import subprocess
import json
import time
import sys
import os
from datetime import datetime

# Force UTF-8 output
if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

def run_cmd(cmd, json_output=False):
    """Run command and return output"""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}
        )
        output = result.stdout + result.stderr
        if json_output:
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return None
        return output.strip()
    except subprocess.TimeoutExpired:
        return None
    except Exception as e:
        return f"Error: {e}"

def check_deployment_status(max_retries=30, retry_interval=5):
    """Check Railway deployment status"""
    print("=" * 60)
    print("FAWN BACKEND DEPLOYMENT MONITOR")
    print("=" * 60)
    print("[{}] Starting deployment status check...\n".format(datetime.now().strftime('%H:%M:%S')))

    for attempt in range(1, max_retries + 1):
        print("[Attempt {}/{}] Checking deployment status...".format(attempt, max_retries))

        # Get project info
        project_info = run_cmd("railway project --json", json_output=True)
        if not project_info:
            print("  [WAIT] Railway CLI not linked. Trying to get status anyway...")
        else:
            print("  Project: {}".format(project_info.get('name', 'N/A')))

        # Get deployment logs (last 20 lines)
        logs = run_cmd("railway logs --tail=20")

        if logs:
            # Check for success indicators
            if any(x in logs.lower() for x in ["success", "running", "ready", "application started"]):
                print("\n[OK] DEPLOYMENT SUCCESSFUL")
                print("\nRecent logs:")
                print("-" * 60)
                print(logs[-500:])  # Last 500 chars
                print("-" * 60)
                return True

            # Check for failure indicators
            elif any(x in logs.lower() for x in ["error", "failed", "exception", "traceback", "crashed"]):
                print("\n[FAIL] DEPLOYMENT FAILED")
                print("\nError logs:")
                print("-" * 60)
                print(logs[-1000:])  # Last 1000 chars
                print("-" * 60)
                return False

            # Still building
            elif "building" in logs.lower() or "deploying" in logs.lower():
                print("  [WAIT] Deployment in progress...")
                print("  Waiting {}s before retry...".format(retry_interval))
                time.sleep(retry_interval)
                continue
            else:
                print("  Status: {}...".format(logs[:100]))
                time.sleep(retry_interval)
                continue
        else:
            print("  [WAIT] Could not fetch logs. Retrying in {}s...".format(retry_interval))
            time.sleep(retry_interval)
            continue

    print("\n[FAIL] DEPLOYMENT CHECK TIMEOUT ({} attempts)".format(max_retries))
    return False

def get_failure_details():
    """Get detailed failure information"""
    print("\n" + "=" * 60)
    print("DIAGNOSTIC INFORMATION")
    print("=" * 60)

    # Get full logs
    print("\nFull recent logs:")
    print("-" * 60)
    logs = run_cmd("railway logs --tail=100")
    if logs:
        print(logs)
    print("-" * 60)

    # Get build status
    print("\nBuild status:")
    status = run_cmd("railway status")
    if status:
        print(status)

    # Get environment variables
    print("\nEnvironment variables:")
    env = run_cmd("railway variables")
    if env:
        print(env[:500])  # First 500 chars

def main():
    """Main monitor function"""
    success = check_deployment_status()

    if not success:
        print("\n" + "=" * 60)
        print("[FAIL] DEPLOYMENT FAILED - IMMEDIATE ACTION REQUIRED")
        print("=" * 60)

        get_failure_details()

        print("\n" + "=" * 60)
        print("NEXT STEPS:")
        print("=" * 60)
        print("1. Review error logs above")
        print("2. Identify root cause")
        print("3. Fix locally:")
        print("   - Check Python syntax: python -m py_compile *.py")
        print("   - Check imports: python -c 'import main'")
        print("   - Check dependencies: pip install -r requirements.txt")
        print("4. Commit and push: git push origin main")
        print("5. Run monitor again: python monitor_deployment.py")
        print("")
        sys.exit(1)
    else:
        print("\n" + "=" * 60)
        print("[OK] DEPLOYMENT COMPLETE AND SUCCESSFUL")
        print("=" * 60)
        print("API is live and ready for testing!")
        print("Health check: https://web-production-13d5b.up.railway.app/health")
        print("")
        sys.exit(0)

if __name__ == "__main__":
    main()

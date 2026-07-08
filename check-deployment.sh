#!/bin/bash
# FAWN Railway Deployment Monitor
# Checks deployment status and alerts on failures
# Usage: ./check-deployment.sh
# Or: bash check-deployment.sh (Windows)

set -e

REPO="fawn-backend"
PROJECT_ID="$(railway project --json 2>/dev/null | jq -r '.id' 2>/dev/null || echo 'unknown')"
MAX_RETRIES=30
RETRY_INTERVAL=5

echo "=============================================="
echo "FAWN Backend Deployment Monitor"
echo "=============================================="
echo "Repository: $REPO"
echo "Checking deployment status..."
echo ""

# Function to check deployment status
check_status() {
  local retry_count=0

  while [ $retry_count -lt $MAX_RETRIES ]; do
    echo "[Attempt $((retry_count + 1))/$MAX_RETRIES] Fetching deployment status..."

    # Get the latest deployment status
    STATUS=$(railway status 2>&1 || echo "UNKNOWN")

    if [[ $STATUS == *"✓"* ]] || [[ $STATUS == *"Success"* ]] || [[ $STATUS == *"running"* ]]; then
      echo "✅ DEPLOYMENT SUCCESSFUL"
      echo "Status: $STATUS"
      return 0
    elif [[ $STATUS == *"✗"* ]] || [[ $STATUS == *"Failed"* ]] || [[ $STATUS == *"ERROR"* ]] || [[ $STATUS == *"error"* ]]; then
      echo "❌ DEPLOYMENT FAILED"
      echo "Status: $STATUS"
      echo ""
      echo "Getting detailed logs..."
      railway logs --tail=50
      return 1
    fi

    echo "Status: $STATUS (waiting for deployment to complete...)"
    echo "Waiting ${RETRY_INTERVAL}s before retry..."
    sleep $RETRY_INTERVAL
    retry_count=$((retry_count + 1))
  done

  echo "❌ DEPLOYMENT CHECK TIMEOUT (exceeded $MAX_RETRIES attempts)"
  echo "Last status: $STATUS"
  return 1
}

# Run the check
if check_status; then
  echo ""
  echo "=============================================="
  echo "✅ DEPLOYMENT COMPLETE AND SUCCESSFUL"
  echo "=============================================="
  exit 0
else
  echo ""
  echo "=============================================="
  echo "❌ DEPLOYMENT FAILED - IMMEDIATE ACTION REQUIRED"
  echo "=============================================="
  echo ""
  echo "Next steps:"
  echo "1. Review logs above for error details"
  echo "2. Identify root cause"
  echo "3. Fix the issue locally"
  echo "4. Commit and push to main"
  echo "5. Run this script again to verify"
  echo ""
  exit 1
fi

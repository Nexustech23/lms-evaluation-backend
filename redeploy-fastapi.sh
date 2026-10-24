#!/usr/bin/env bash
# Run: bash redeploy-fastapi.sh in /home/ubuntu/lms-evaluation-backend-new.
# For the current inline queue mode; wait for active jobs before redeploying.
set -Eeuo pipefail
cd "$(dirname "$0")"

exec 9>".git/lms-deploy.lock"
flock -n 9 || { echo "Another deployment is running." >&2; exit 1; }
if [ "$(git branch --show-current)" != main ]; then
  echo "Switch to main before deploying." >&2
  exit 1
fi
git pull --ff-only origin main

# Stamp the image that is built, rather than guessing a release inside the container.
export APP_GIT_COMMIT
APP_GIT_COMMIT=$(git rev-parse HEAD)
export APP_SOURCE_DIRTY=false
if [ -n "$(git status --porcelain)" ]; then
  APP_SOURCE_DIRTY=true
fi

SERVICE=lms-backend-fastapi
ROLLBACK_IMAGE=local/lms-backend-fastapi:rollback
BACKUP_IMAGE=local/lms-backend-fastapi:deploy-backup
CACHE_KEEP="${BUILD_CACHE_KEEP:-2GB}"
old_image=$(docker inspect --format '{{.Image}}' "$SERVICE" 2>/dev/null || true)
old_rollback=$(docker image inspect --format '{{.Id}}' "$ROLLBACK_IMAGE" 2>/dev/null || true)
if [ -n "$old_image" ]; then
  docker image tag "$old_image" "$BACKUP_IMAGE"
fi
trap 'echo "Deployment stopped. Check the error above; retain rollback/backup images for recovery." >&2' ERR

echo "==> Building backend..."
docker compose build "$SERVICE"
docker compose up -d --no-build --no-deps "$SERVICE"

healthy=false
for attempt in {1..30}; do
  if [ "$(docker inspect --format '{{.State.Running}}' "$SERVICE")" = true ] &&
    docker compose exec -T "$SERVICE" python -c '
import json
import urllib.request
with urllib.request.urlopen("http://127.0.0.1:5050/health", timeout=5) as response:
    status = json.load(response)
assert status.get("status") == "healthy" and status.get("database") == "connected"
' >/dev/null 2>&1; then
    healthy=true
    break
  fi
  sleep 2
done
if [ "$healthy" != true ]; then
  echo "Backend/database health check failed. No cleanup performed. Backup: $BACKUP_IMAGE" >&2
  exit 1
fi

new_image=$(docker inspect --format '{{.Image}}' "$SERVICE")
if [ -n "$old_image" ] && [ "$old_image" != "$new_image" ]; then
  docker image tag "$old_image" "$ROLLBACK_IMAGE"
fi
if [ -n "$old_image" ]; then
  docker image rm --no-prune "$BACKUP_IMAGE" >/dev/null
fi
rollback_image=$(docker image inspect --format '{{.Id}}' "$ROLLBACK_IMAGE" 2>/dev/null || true)
if [ -n "$old_rollback" ] && [ "$old_rollback" != "$rollback_image" ] && [ "$old_rollback" != "$new_image" ]; then
  # Only the superseded rollback from this project, without force deletion.
  docker image rm --no-prune "$old_rollback" || echo "Old image is still referenced; retained."
fi

echo "==> Healthy. Cleaning unused build cache (shared Docker cache; keep $CACHE_KEEP)..."
docker builder prune -af --keep-storage "$CACHE_KEEP" || echo "Cache cleanup failed; deployment remains healthy."
echo "==> Backend deployed. Previous release retained as $ROLLBACK_IMAGE (when available)."
df -h /

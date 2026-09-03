# Pinned to bookworm (Debian 12), not the floating "slim" tag — "slim"
# now resolves to trixie (Debian 13), which Playwright's dependency
# installer below doesn't support yet (it falls back to guessing Ubuntu
# 20.04 package names that don't exist in trixie's repos and the build
# fails). bookworm is on Playwright's supported OS list.
FROM python:3.11-slim-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright (app/services/pdf_render.py) needs its browser binary installed
# separately from the pip package — without this, PDF rendering fails in
# the container with "Executable doesn't exist" the first time it's used.
# --with-deps also pulls in the OS-level libraries Chromium needs.
RUN playwright install --with-deps chromium

COPY . .

EXPOSE 5050

# Multi-process: gunicorn manages N uvicorn workers in one container, so a
# slow request no longer blocks every other request. Worker count is
# WEB_CONCURRENCY (default 3) — set it to ~2*vCPU+1. Shell form is used so
# ${WEB_CONCURRENCY:-3} expands.
#
# --timeout 900: with QUEUE_MODE=inline (the default) the AI / PDF /
# transcript jobs run inside the request that started them, so gunicorn
# must not kill the worker mid-job. 15 min covers the longest single job
# (grading + transcript, roadmap generation). When QUEUE_MODE=redis and
# the lms-worker container handles jobs, this can be lowered again.
CMD gunicorn app.main:app \
    -k uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:5050 \
    --workers ${WEB_CONCURRENCY:-3} \
    --timeout 900 \
    --graceful-timeout 60 \
    --access-logfile - \
    --error-logfile -

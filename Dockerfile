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

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5050"]

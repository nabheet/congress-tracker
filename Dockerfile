FROM python:3.14-slim

# uv — copy pinned binary from the official image
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /uvx /bin/

# supercronic — cron daemon for containers (runs as PID 1, logs to stdout).
# Digest verified at build time (sha256 of the v0.2.49 release binary).
ADD --checksum=sha256:a53ae236602c7338aba3fbaff40bda6300eae3b9fedb8261eb06cfe3724430c1 \
    https://github.com/aptible/supercronic/releases/download/v0.2.49/supercronic-linux-amd64 \
    /usr/local/bin/supercronic
RUN chmod +x /usr/local/bin/supercronic

WORKDIR /app
COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-dev

# uv sync installs into /app/.venv; make its python3 the default
ENV PATH="/app/.venv/bin:$PATH"

COPY pull_congress.py /app/
COPY crontab /app/crontab
COPY migrations/ /app/migrations/

# Daily 08:00 UTC (Senate eFD files during US business hours ET)
CMD ["/usr/local/bin/supercronic", "/app/crontab"]

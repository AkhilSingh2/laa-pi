# laa-pi: dashboard + scheduler + a real (headed) Chromium on a virtual display, viewable in the browser.
# One public port ($PORT, default 8080) served by Caddy behind a password:
#   /          -> Streamlit dashboard
#   /browser/  -> noVNC view of the server-side browser (log in to LinkedIn / Naukri, watch the copilot)
# Persist /app/data (DB, browser profile with your logged-in sessions, resumes, settings) on a volume.
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Kolkata \
    DISPLAY=:99 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_LINK_MODE=copy \
    PATH=/app/.venv/bin:$PATH \
    PORT=8080 \
    BROWSER_VIEW_URL="/browser/vnc.html?autoconnect=1&resize=scale&reconnect=1&path=browser/websockify"

RUN apt-get update && apt-get install -y --no-install-recommends \
        supervisor xvfb x11vnc novnc websockify \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libharfbuzz-subset0 libffi8 \
        fonts-dejavu-core fonts-liberation fonts-noto-core \
        tzdata curl ca-certificates libatomic1 \
    && ln -sf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY --from=caddy:2 /usr/bin/caddy /usr/bin/caddy
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

RUN useradd --create-home --uid 1000 app
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project \
    && playwright install --with-deps chromium \
    && rm -rf /root/.cache /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright

COPY . .
RUN mkdir -p /app/seed && (cp data/master_profile.json /app/seed/master_profile.json 2>/dev/null \
        || cp data/master_profile.example.json /app/seed/master_profile.json) \
    && chmod +x deploy/entrypoint.sh \
    && chown -R app:app /app

EXPOSE 8080
VOLUME ["/app/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

ENTRYPOINT ["/app/deploy/entrypoint.sh"]

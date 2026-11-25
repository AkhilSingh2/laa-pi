#!/usr/bin/env bash
# Container start: require a dashboard password, prepare the data volume, clear locks left by the previous
# container, then hand over to supervisord (Xvfb, VNC, noVNC, Streamlit, scheduler, Caddy).
set -euo pipefail

if [ -z "${APP_PASSWORD:-}" ]; then
    echo "APP_PASSWORD is not set. The dashboard and the browser view are reachable from the internet," >&2
    echo "so the container refuses to start without a password. Set APP_PASSWORD and restart." >&2
    exit 1
fi
APP_PASSWORD_HASH="$(caddy hash-password --plaintext "$APP_PASSWORD")"
export APP_PASSWORD_HASH
unset APP_PASSWORD

DATA=/app/data
mkdir -p "$DATA"/exports "$DATA"/resumes "$DATA"/screenshots "$DATA"/browser_profile
if [ ! -f "$DATA/master_profile.json" ]; then
    cp /app/seed/master_profile.json "$DATA/master_profile.json"
    echo "Seeded $DATA/master_profile.json from the image."
fi

# PIDs restart from 1 in a new container, so pid-based locks from the old one could look alive. Clear them.
rm -f "$DATA/exports/browser.lock" /tmp/.X99-lock /tmp/.X11-unix/X99
mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
rm -f "$DATA"/browser_profile/Singleton{Lock,Cookie,Socket}
if [ -f "$DATA/exports/browser_run.json" ]; then
    /app/.venv/bin/python - <<'PY'
from core.settings import load_settings
from pipeline.browser_run import ACTIVE_STATES, read_state, write_state

s = load_settings()
if read_state(s).get("state") in ACTIVE_STATES:
    write_state(s, state="failed", error="container restarted during collection; fetched jobs were kept")
PY
fi

if [ "$(stat -c %u "$DATA")" != "1000" ] || [ "$(stat -c %u "$DATA/browser_profile")" != "1000" ]; then
    chown -R app:app "$DATA"
fi

exec /usr/bin/supervisord -c /app/deploy/supervisord.conf

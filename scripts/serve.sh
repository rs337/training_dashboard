#!/bin/bash
# Start the dashboard on localhost:8000 with the settings in .env.
# Used by hand (./scripts/serve.sh) and by the launchd start-at-login job.
# It listens on 127.0.0.1 only; phone access goes through `tailscale serve`.
cd "$(dirname "$0")/.." || exit 1
exec uv run --env-file .env uvicorn app.main:app --host 127.0.0.1 --port 8000

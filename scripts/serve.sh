#!/usr/bin/env bash
# Run the inbound webhook + approval UI (mock LLM unless OPENAI_API_KEY is set in .env).
set -euo pipefail
cd "$(dirname "$0")/.."
exec ${PYTHON:-python3} -m uvicorn --factory agent_approval.web:create_app --host 0.0.0.0 --port "${PORT:-8800}"

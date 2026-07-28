"""
api/settings.py

API-layer configuration, kept separate from config/settings.py so the web
layer's concerns (CORS, host/port) don't get mixed into the agent's own
config module.
"""

import os

# Comma-separated list of origins allowed to call this API, e.g.
#   FRONTEND_ORIGINS="https://compliance.mycompany.com,http://localhost:5500"
# Defaults to localhost dev origins for the plain-HTML frontend in
# frontend/index.html served via `python -m http.server`.
_default_origins = "http://localhost:5500,http://127.0.0.1:5500,http://localhost:3000"
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("FRONTEND_ORIGINS", _default_origins).split(",") if o.strip()]

API_HOST = os.environ.get("API_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("API_PORT", "8000"))

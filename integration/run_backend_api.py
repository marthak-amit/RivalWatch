# Test harness only: starts the real backend app, allowing the stand-in competitor site through the SSRF guard
# (the backend's own tests do the same by editing ALLOW_LOCAL).
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "src"))
from competitor_moves.core import ssrf
ssrf.ALLOW_LOCAL.update(x for x in os.environ.get("HARNESS_ALLOW_LOCAL", "").split(",") if x)
import uvicorn
uvicorn.run("competitor_moves.api.main:app", host="127.0.0.1", port=8000, log_level="warning")

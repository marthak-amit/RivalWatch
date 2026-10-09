import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "src"))
from competitor_moves.core import ssrf
ssrf.ALLOW_LOCAL.update(x for x in os.environ.get("HARNESS_ALLOW_LOCAL", "").split(",") if x)
from competitor_moves.workers.worker import main
main()

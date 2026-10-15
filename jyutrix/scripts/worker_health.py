import sys
import time
from backend.app.database import transaction, Heartbeat

with transaction() as db:
    beat = db.get(Heartbeat, "worker")
    sys.exit(0 if beat and beat.seen > time.time() - 60 else 1)

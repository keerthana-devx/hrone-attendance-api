import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Ensure test DB is set before importing app so create_indexes() targets the right DB
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ["MONGO_DB"] = "attendance_test_db"

from app.main import create_indexes  # noqa: E402

# Create indexes in the test DB once at collection time (lifespan runs per-request
# in TestClient but indexes must exist before the first test that needs them)
create_indexes()

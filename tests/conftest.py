import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)  # dashboard.py loads templates relative to the repo root

# Point at an unused port so nothing accidentally talks to a real database.
os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=200")
os.environ.setdefault("DASHBOARD_SECRET_KEY", "test-secret")

"""Run a release import, overview and SQLite smoke on disposable synthetic inputs."""
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

with tempfile.TemporaryDirectory() as state:
    os.environ["PM_PRIVATE_DIR"] = state
    import app
    from pm import ai_store

    app.page()
    with ai_store.database() as db:
        print(db.execute("PRAGMA user_version").fetchone()[0])

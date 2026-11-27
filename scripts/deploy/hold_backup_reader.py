"""Keep SQLite's WAL sidecars available to the independent read-only mount."""
import os
import signal
import socket
import sqlite3
from contextlib import closing


def main():
    address = os.environ["NOTIFY_SOCKET"]
    if address.startswith("@"):
        address = "\0" + address[1:]
    # Only the application identity may initialize SQLite's shared memory.
    # Hold a connection, without a transaction, while pm-backup reads the snapshot.
    with closing(sqlite3.connect("file:/var/lib/paradiigma/db/ai.sqlite3?mode=rw",
                                uri=True, timeout=5)) as database:
        database.execute("PRAGMA query_only=ON")
        database.execute("SELECT name FROM sqlite_schema LIMIT 1").fetchone()
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify:
            notify.sendto(b"READY=1", address)
        signal.pause()


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, sqlite3.Error):
        raise SystemExit("SQLite backup reader failed to initialize") from None

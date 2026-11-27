"""Check Git index blobs before publication. Never emit matched text."""
import re
import subprocess
import sys
from pathlib import PurePosixPath

PRIVATE_NAME = re.compile(r"(?:^|/)(?:private|credentials|secrets|backups|\.ssh|\.aws)(?:/|$)|"
                         r"(?:^|/)\.env(?:$|\.(?!example$).+)|"
                         r"\.(?:db|sqlite3?|pem|key|p12|pfx|gguf|zip|tar|xlsx|docx|pptx|pdf|log)(?:$|-(?:wal|shm|journal)$)", re.I)
TOKEN = re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----|"
                   rb"\b(?:gh[pousr]_[a-zA-Z0-9]{30,}|github_pat_[a-zA-Z0-9_]{40,}|"
                   rb"(?:AKIA|ASIA)[A-Z0-9]{16}|y0_[A-Za-z0-9_-]{32,}|AQVN[A-Za-z0-9_-]{32,})\b|"
                   rb"(?im:^[ \t]*(?:export[ \t]+)?(?:METRIKA_TOKEN|YANDEX_SEARCH_API_KEY|AWS_SECRET_ACCESS_KEY)"
                   rb"[ \t]*=[ \t]*[\"']?[A-Za-z0-9_./+=-]{16,})")


def main():
    entries = subprocess.check_output(["git", "ls-files", "--stage", "-z"]).split(b"\0")
    bad = False
    for item in filter(None, entries):
        meta, raw = item.split(b"\t", 1)
        mode, oid, stage = meta.split()
        path = str(PurePosixPath(raw.decode("utf8", errors="replace")))
        if stage != b"0" or mode not in (b"100644", b"100755"):
            issue = "unmerged or non-regular entry"
        elif PRIVATE_NAME.search(path):
            issue = "private artifact filename"
        elif int(subprocess.check_output(["git", "cat-file", "-s", oid])) > 5_000_000:
            issue = "oversized artifact needs review"
        elif TOKEN.search(subprocess.check_output(["git", "cat-file", "blob", oid])):
            issue = "credential pattern"
        else:
            continue
        print(f"{ascii(path)}: {issue}", file=sys.stderr)
        bad = True
    print("index scan: " + ("failed" if bad else "passed"))
    return int(bad)


if __name__ == "__main__":
    raise SystemExit(main())

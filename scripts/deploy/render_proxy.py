"""Render the HTTPS vhost for a reviewed frontend release to stdout."""

import argparse
import base64
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def render(domain, mode):
    labels = domain.split(".")
    if (
        len(domain) > 253 or len(labels) < 2 or labels[-1].isdigit()
        or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", s) for s in labels)
    ):
        raise ValueError("Use a lower-case DNS name without scheme, wildcard or port.")
    if mode not in {"demo", "production"}:
        raise ValueError("Select demo or production.")
    template = (ROOT / "deploy/nginx/paradiigma.conf.in").read_text(encoding="utf-8")
    script = (ROOT / "web/app.js").read_text(encoding="utf-8").encode()
    digest = base64.b64encode(hashlib.sha256(script).digest()).decode("ascii")
    import_rule = "location = /api/import { return 403; }" if mode == "demo" else ""
    return template.replace("@DOMAIN@", domain).replace("@SCRIPT_HASH@", digest).replace(
        "@DEMO_IMPORT_RULE@", import_rule
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("domain")
    parser.add_argument("--mode", required=True, choices=("production", "demo"))
    args = parser.parse_args()
    try:
        print(render(args.domain, args.mode), end="")
    except ValueError as exc:
        parser.error(str(exc))

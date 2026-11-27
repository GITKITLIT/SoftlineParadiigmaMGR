"""Obtain the pinned official GGUF, verify its digest, and run local inference."""

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pm.settings import private_dir

REV = "bc640142c66e1fdd12af0bd68f40445458f3869b"
NAME = "Qwen3-4B-Q4_K_M.gguf"
DIGEST = "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5"
URL = f"https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/{REV}/{NAME}"
parser = argparse.ArgumentParser()
parser.add_argument("--server", default="llama-server")
parser.add_argument("--model", type=Path, default=private_dir() / NAME)
parser.add_argument("--download", action="store_true")
parser.add_argument("--parallel", type=int, choices=(1, 2), default=1)
parser.add_argument("--gpu-layers", type=int, default=0)
parser.add_argument("--threads-batch", type=int, default=8)
args = parser.parse_args()
binary = shutil.which(args.server)
if not binary:
    sys.exit("Установите llama.cpp или укажите --server с путём к llama-server.")


def checksum(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


if not args.model.exists():
    if not args.download:
        sys.exit("Для первой загрузки весов добавьте --download (2,50 ГБ).")
    args.model.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.model.with_suffix(".download")
    with urlopen(URL, timeout=90) as source, tmp.open("wb") as dst:
        shutil.copyfileobj(source, dst, 1024 * 1024)
    if checksum(tmp) != DIGEST:
        sys.exit("Контрольная сумма загрузки не совпала.")
    tmp.replace(args.model)
elif checksum(args.model) != DIGEST:
    sys.exit("Неверная контрольная сумма GGUF.")
try:
    capability = subprocess.run([binary, "--help"], capture_output=True, timeout=10, check=True)
except (OSError, subprocess.SubprocessError):
    sys.exit("Не удалось проверить параметры llama.cpp.")
help_text = (capability.stdout + capability.stderr).decode("utf-8", errors="replace")

command = [
    binary, "--model", str(args.model), "--alias", "qwen3-4b",
    "--host", "127.0.0.1", "--port", "18081",
    "--ctx-size", str(args.parallel * 8192), "--parallel", str(args.parallel),
    "--threads", "4", "--threads-batch", str(args.threads_batch), "--gpu-layers", str(args.gpu_layers),
    "--chat-template-kwargs", '{"enable_thinking":false}',
]
if re.search(r"(?<!\w)--reasoning(?=[\s,=])", help_text):
    command += ["--reasoning", "off"]
if re.search(r"(?<!\w)--cache-ram(?=[\s,=])", help_text):
    command += ["--cache-ram", "512"]
else:
    print("Этот llama.cpp не поддерживает --cache-ram. Для лимита 512 МиБ нужен совместимый runtime.",
          file=sys.stderr)
sys.exit(subprocess.call(command))

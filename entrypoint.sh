#!/bin/bash
set -euo pipefail

# 容器重启时，/tmp 可能保留上一次运行遗留的 X lock 文件，导致：
# "Server is already active for display 99"
# 这里使用 xvfb-run -a 自动选择可用 display，并由其负责清理。
rm -f /tmp/.X*-lock /tmp/.X11-unix/X* 2>/dev/null || true

python - <<'PY'
import json
import os
from pathlib import Path

# The reverse proxy can mount this app under "/<PATH_PREFIX>/*".
# We expose the prefix to the SPA at runtime so it can build correct API URLs.
raw = (os.getenv("PATH_PREFIX") or "").strip()
prefix = raw.strip("/")  # allow "", "gemini2api", "/gemini2api/"
base_path = f"/{prefix}" if prefix else ""

cfg = {
    "basePath": base_path,
    "apiBase": base_path,
}

out = Path("static/runtime-config.js")
out.write_text(
    "// Generated at container startup. Do not edit.\n"
    "window.__G2API_CONFIG__ = "
    + json.dumps(cfg, ensure_ascii=True, separators=(",", ":"))
    + ";\n",
    encoding="utf-8",
)
PY

exec xvfb-run -a -s "-screen 0 1280x800x24 -ac" python -u main.py

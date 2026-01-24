#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="$ROOT_DIR/bin"
DEST_BIN="${MIHOMO_BIN:-$BIN_DIR/mihomo}"
DEST_DIR="$(dirname -- "$DEST_BIN")"

OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"

case "$ARCH" in
  x86_64|amd64) ARCH_KEY="amd64" ;;
  aarch64|arm64) ARCH_KEY="arm64" ;;
  armv7l|armv7) ARCH_KEY="armv7" ;;
  *) echo "unsupported arch: $ARCH" >&2; exit 1 ;;
esac

API_URL="https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"

echo "Detect: os=$OS arch=$ARCH_KEY"
echo "Fetch: $API_URL"

json_path="$(mktemp)"
trap 'rm -f "$json_path"' EXIT
curl -fsSL "$API_URL" -o "$json_path"

asset_url="$(
  python3 - <<'PY' "$json_path" "$OS" "$ARCH_KEY"
import json, sys
path, os_key, arch_key = sys.argv[1:4]
j = json.load(open(path, "r", encoding="utf-8"))
assets = j.get("assets", [])
tag = j.get("tag_name", "")

def pick(preds):
  for a in assets:
    name = a.get("name", "")
    url = a.get("browser_download_url")
    if not url:
      continue
    if all(p(name) for p in preds):
      return url
  return None

want = [
  lambda n: os_key in n,
  lambda n: arch_key in n,
  lambda n: n.endswith(".gz"),
]

# Prefer a plain build first; on linux/amd64 prefer "compatible" if present.
url = None
if os_key == "linux" and arch_key == "amd64":
  url = pick(want + [lambda n: "compatible" in n])
  if not url:
    url = pick(want + [lambda n: "compatible" not in n])
else:
  url = pick(want)

if not url:
  print("ERROR: cannot find matching asset", file=sys.stderr)
  print("tag:", tag, file=sys.stderr)
  for a in assets[:30]:
    print(a.get("name",""), file=sys.stderr)
  sys.exit(2)

print(url)
PY
)"

echo "Download: $asset_url"
mkdir -p "$DEST_DIR"

tmp_gz="$(mktemp)"
trap 'rm -f "$tmp_gz"' EXIT
curl -L --fail "$asset_url" -o "$tmp_gz"

if [ -f "$DEST_BIN" ]; then
  mv -f "$DEST_BIN" "$DEST_BIN.bak"
fi

gunzip -c "$tmp_gz" > "$DEST_BIN"
chmod +x "$DEST_BIN"

echo "OK: $DEST_BIN"
echo "Version:"
"$DEST_BIN" -v | head -n 1

#!/bin/bash
# HomeTube installer for Debian, Ubuntu, Armbian and Raspberry Pi OS (anything with apt and systemd).
# Run as root from a clone of the repository:
#
#   sudo ./install.sh                       # the TV page on port 8890 of this board
#   sudo ./install.sh --route-table 200 --route-dev wg0 --after wg-quick@wg0.service
#                                           # HomeTube's own traffic only through a VPN tunnel's table
#
# Options:
#   --bind ADDR        address to listen on (default 0.0.0.0: the TV on your network can reach it)
#   --port N           port (default 8890)
#   --route-table N    policy-routing table for HomeTube's traffic (your tunnel's; give it an
#                      unreachable fallback route so it fails closed)
#   --route-pref N     priority of that rule (default 91)
#   --route-dev IFACE  the tunnel interface: HomeTube checks before every request to YouTube that its
#                      traffic leaves by it, and refuses otherwise (fails closed)
#   --after UNIT       start after this unit, e.g. the tunnel's
#   --label TEXT       shown after "HomeTube" in the page title
#   --deno PATH        use this deno binary instead of downloading the latest release
#   --vpn FILE         optional: HomeTube's own WireGuard tunnel from your provider's configs (a .conf, or a
#                      .zip of many servers; any provider). HomeTube needs no VPN; without this it goes out directly
#
# Running it again updates the program and keeps the settings and everything you saved.
set -euo pipefail
ORIG_PWD=$PWD
cd "$(dirname "$(readlink -f "$0")")"

APP=/opt/hometube
ETC=/etc/hometube
BIND= PORT= TABLE= PREF= DEV= AFTER= LABEL= DENO_FROM= VPN_FILE=
while [ $# -gt 0 ]; do
  case "$1" in
    --bind) BIND=$2; shift 2;;
    --port) PORT=$2; shift 2;;
    --route-table) TABLE=$2; shift 2;;
    --route-pref) PREF=$2; shift 2;;
    --route-dev) DEV=$2; shift 2;;
    --after) AFTER=$2; shift 2;;
    --label) LABEL=$2; shift 2;;
    --deno) DENO_FROM=$2; shift 2;;
    --vpn) VPN_FILE=$(cd "$ORIG_PWD" && readlink -f "$2"); [ -f "$VPN_FILE" ] || { echo "no such file: $2" >&2; exit 2; }; shift 2;;
    -h|--help) sed -n '2,25p' "$0"; exit 0;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2;;
  esac
done

[ "$(id -u)" = 0 ] || { echo "run as root: sudo $0" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "HomeTube needs systemd" >&2; exit 1; }
command -v apt-get >/dev/null || { echo "this installer uses apt (Debian family); see README for other systems" >&2; exit 1; }

echo "[1/5] packages"
DEBIAN_FRONTEND=noninteractive apt-get update -qq   # a fresh system has no package lists yet
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-venv ca-certificates curl unzip iproute2 >/dev/null

echo "[2/5] program files in $APP"
install -d -m 0755 "$APP" "$APP/web"
install -m 0644 hometube.py "$APP/hometube.py"
for f in web/*; do install -m 0644 "$f" "$APP/web/$(basename "$f")"; done

echo "[3/5] yt-dlp (in its own environment)"
[ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --quiet --upgrade pip "yt-dlp[default]"

echo "[4/5] deno (runs YouTube's JavaScript checks for yt-dlp)"
if [ -n "$DENO_FROM" ]; then
  install -m 0755 "$DENO_FROM" "$APP/deno"
elif [ ! -x "$APP/deno" ]; then
  case "$(uname -m)" in
    aarch64|arm64) target=aarch64-unknown-linux-gnu;;
    x86_64|amd64) target=x86_64-unknown-linux-gnu;;
    *) echo "no deno build for $(uname -m): install deno yourself and pass --deno PATH" >&2; exit 1;;
  esac
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
  base="https://github.com/denoland/deno/releases/latest/download/deno-$target.zip"
  curl -fsSL --retry 3 -o "$tmp/deno.zip" "$base"
  curl -fsSL --retry 3 -o "$tmp/deno.zip.sha256sum" "$base.sha256sum"
  want=$(awk '{print $1; exit}' "$tmp/deno.zip.sha256sum")
  have=$(sha256sum "$tmp/deno.zip" | awk '{print $1}')
  [ -n "$want" ] && [ "$want" = "$have" ] || { echo "deno download does not match its published checksum" >&2; exit 1; }
  unzip -q -o "$tmp/deno.zip" -d "$tmp"
  install -m 0755 "$tmp/deno" "$APP/deno"
fi
"$APP/deno" --version | head -1

echo "[5/5] settings, service and daily yt-dlp update"
install -d -m 0755 "$ETC"
# settings: kept from an earlier install; options given now are written over them
HT_BIND=$BIND HT_PORT=$PORT HT_TABLE=$TABLE HT_PREF=$PREF HT_DEV=$DEV HT_AFTER=$AFTER HT_LABEL=$LABEL python3 - "$ETC/config.json" <<'PY'
import json, os, sys
path = sys.argv[1]
try:
    cfg = json.load(open(path))
except FileNotFoundError:
    cfg = {'bind': '0.0.0.0', 'port': 8890}
env = os.environ
if env['HT_BIND']: cfg['bind'] = env['HT_BIND']
if env['HT_PORT']: cfg['port'] = int(env['HT_PORT'])
if env['HT_TABLE']: cfg['route_table'] = int(env['HT_TABLE'])
if env['HT_PREF']: cfg['route_pref'] = int(env['HT_PREF'])
if env['HT_DEV']: cfg['route_dev'] = env['HT_DEV']
if env['HT_AFTER']: cfg['after_unit'] = env['HT_AFTER']
if env['HT_LABEL']: cfg['label'] = env['HT_LABEL']
tmp = path + '.new'
with open(tmp, 'w') as f:
    json.dump(cfg, f, indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
os.chmod(tmp, 0o644); os.replace(tmp, path)
PY
# check the settings before anything starts (a bad value stops here with the reason)
HOMETUBE_CONFIG="$ETC/config.json" python3 -c "import sys; sys.path.insert(0, '$APP'); import hometube" >/dev/null
sync   # small boards often write to flash late: make sure nothing is lost to a power cut
HOMETUBE_CONFIG="$ETC/config.json" python3 "$APP/hometube.py" install-units
if [ -n "$VPN_FILE" ]; then
  echo "[+] HomeTube's own VPN from $(basename "$VPN_FILE")"
  command -v wg-quick >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends wireguard-tools >/dev/null
  HOMETUBE_CONFIG="$ETC/config.json" python3 "$APP/hometube.py" vpn import "$VPN_FILE"
fi

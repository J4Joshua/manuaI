#!/usr/bin/env bash
#
# Point the ManuAI iOS app at THIS Mac's LAN/hotspot IP and open it in Xcode.
#
# The glasses app streams mic audio to src/glasses_bridge.py on port 8766. The host
# is a single Swift constant; this script keeps it in sync so you never hand-edit it.
# Re-run it whenever your IP changes (new WiFi, DHCP renew, switching to the iPhone
# hotspot).
#
# Usage:
#   ios/configure_and_launch.sh               # auto-detect IP (en0, then en1)
#   ios/configure_and_launch.sh 172.20.10.2   # force an IP (e.g. iPhone Personal Hotspot)
#
set -euo pipefail

PORT=8766
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VM="$DIR/ManuAI/ViewModels/StreamSessionViewModel.swift"
PROJ="$DIR/ManuAI.xcodeproj"

# 1. Resolve the IP (CLI arg wins; else en0 WiFi, then en1).
IP="${1:-}"
if [ -z "$IP" ]; then
  IP="$(ipconfig getifaddr en0 2>/dev/null || true)"
  [ -z "$IP" ] && IP="$(ipconfig getifaddr en1 2>/dev/null || true)"
fi
if [ -z "$IP" ]; then
  echo "✗ No LAN IP found (en0/en1 down). Join WiFi or the iPhone hotspot, or pass one: $0 <ip>" >&2
  exit 1
fi
HOST="ws://${IP}:${PORT}"

# 2. Rewrite the single host constant (idempotent).
[ -f "$VM" ] || { echo "✗ Not found: $VM" >&2; exit 1; }
sed -i '' -E "s#^private let streamPublishHost = \".*\"#private let streamPublishHost = \"${HOST}\"#" "$VM"
grep -q "streamPublishHost = \"${HOST}\"" "$VM" || { echo "✗ Could not update streamPublishHost in $VM" >&2; exit 1; }
echo "✓ iOS host → ${HOST}"
echo "  Run the bridge on this Mac:  .venv/bin/python src/glasses_bridge.py"

# 3. Open the project.
open "$PROJ"
echo "✓ Opened ManuAI.xcodeproj — set Signing Team (target ManuAI → Signing & Capabilities),"
echo "  Run on the iPhone, then tap 'Start hands-free (audio only)'."

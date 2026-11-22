#!/usr/bin/env bash
set -euo pipefail

install -D -m 0644 ghost_hunter_v2.py "$STAGE$APP_INSTALL_DIR/ghost_hunter_v2.py"

# APPLaunch shows "Loading..." and then stops drawing so the child owns the
# panel. A Wayland or KMS window never replaces that frame. Draw to the
# framebuffer the launcher just released.
cat >"$STAGE$INSTALL_PREFIX/bin/$PKG_NAME" <<'EOF'
#!/bin/sh
LOG=/tmp/ghost-hunter.log
: >"$LOG" 2>/dev/null || LOG=/dev/null
FB="${GHOST_HUNTER_FB:-/dev/fb0}"
if [ -e "$FB" ]; then
  export GHOST_HUNTER_FB="$FB"
  export SDL_VIDEODRIVER=dummy
  export SDL_AUDIODRIVER=dummy
fi
echo "[ghost-hunter] fb=${GHOST_HUNTER_FB:-none} sdl=${SDL_VIDEODRIVER:-default} uid=$(id -u)" >>"$LOG" 2>&1
exec python3 /usr/share/APPLaunch/apps/ghost-hunter/ghost_hunter_v2.py "$@" >>"$LOG" 2>&1
EOF
chmod 0755 "$STAGE$INSTALL_PREFIX/bin/$PKG_NAME"

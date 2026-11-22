#!/usr/bin/env bash
set -euo pipefail

install -D -m 0644 ghost_hunter_v2.py "$STAGE$APP_INSTALL_DIR/ghost_hunter_v2.py"

# labwc owns DRM on the CardputerZero. pygame/SDL has to take the Wayland
# socket when APPLaunch starts this as user pi, and only fall back to kmsdrm
# when that compositor is not running.
cat >"$STAGE$INSTALL_PREFIX/bin/$PKG_NAME" <<'EOF'
#!/bin/sh
LOG=/tmp/ghost-hunter.log
: >"$LOG" 2>/dev/null || LOG=/dev/null

if [ -z "${XDG_RUNTIME_DIR:-}" ]; then
  _uid=$(id -u 2>/dev/null || echo 1000)
  if [ -d "/run/user/$_uid" ]; then
    XDG_RUNTIME_DIR="/run/user/$_uid"
  elif [ -d "/run/user/1000" ]; then
    XDG_RUNTIME_DIR="/run/user/1000"
  fi
  [ -n "$XDG_RUNTIME_DIR" ] && export XDG_RUNTIME_DIR
fi

_wl_ok=0
if [ -n "${WAYLAND_DISPLAY:-}" ] && [ -n "${XDG_RUNTIME_DIR:-}" ] && \
   [ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ]; then
  _wl_ok=1
elif [ -n "${XDG_RUNTIME_DIR:-}" ]; then
  for _c in wayland-0 wayland-1; do
    if [ -S "$XDG_RUNTIME_DIR/$_c" ]; then
      WAYLAND_DISPLAY=$_c
      export WAYLAND_DISPLAY
      _wl_ok=1
      break
    fi
  done
fi

if [ -z "${SDL_VIDEODRIVER:-}" ]; then
  if [ "$_wl_ok" = 1 ]; then
    SDL_VIDEODRIVER=wayland
  elif [ -e /dev/dri/card0 ]; then
    SDL_VIDEODRIVER=kmsdrm
  fi
  [ -n "${SDL_VIDEODRIVER:-}" ] && export SDL_VIDEODRIVER
fi

echo "[ghost-hunter] driver=${SDL_VIDEODRIVER:-default} WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-} uid=$(id -u)" >>"$LOG" 2>&1
exec python3 /usr/share/APPLaunch/apps/ghost-hunter/ghost_hunter_v2.py "$@" >>"$LOG" 2>&1
EOF
chmod 0755 "$STAGE$INSTALL_PREFIX/bin/$PKG_NAME"

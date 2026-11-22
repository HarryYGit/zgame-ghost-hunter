#!/bin/sh
# Build the Ghost Hunter .deb on a CardputerZero and install it.
set -eu

SRC="$(mktemp -d)"
STAGE="$(mktemp -d)"
cleanup() { rm -rf "$SRC" "$STAGE"; }
trap cleanup EXIT

echo "Downloading Ghost Hunter..."
curl -fsSL -o "$SRC/src.tgz" https://github.com/HarryYGit/zgame-ghost-hunter/archive/refs/heads/main.tar.gz
tar -xzf "$SRC/src.tgz" -C "$SRC" --strip-components=1
APP="$SRC/ghost-hunter-v2"
cd "$APP"

# shellcheck disable=SC1091
. ./packaging/meta.env

echo "Installing runtime packages..."
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update
sudo apt-get install -y python3 python3-pygame python3-numpy python3-spidev alsa-utils fonts-dejavu-core

PREFIX=/usr/share/APPLaunch
export STAGE INSTALL_PREFIX="$PREFIX" APP_INSTALL_DIR="$PREFIX/apps/$PKG_NAME" PKG_NAME

mkdir -p \
  "$STAGE/DEBIAN" \
  "$STAGE$PREFIX/applications" \
  "$STAGE$PREFIX/bin" \
  "$STAGE$PREFIX/share/images" \
  "$STAGE$APP_INSTALL_DIR"

echo "Packing $PKG_NAME $PKG_VERSION..."
bash packaging/build.sh
bash packaging/stage.sh
cp packaging/icon.png "$STAGE$PREFIX/share/images/${PKG_NAME}.png"
cp packaging/postinst "$STAGE/DEBIAN/postinst"
chmod 0755 "$STAGE/DEBIAN/postinst"

cat > "$STAGE$PREFIX/applications/${PKG_NAME}.desktop" <<EOF
[Desktop Entry]
Name=$APP_NAME
Exec=$APP_EXEC
Terminal=$APP_TERMINAL
Sysplause=${APP_SYSPLAUSE:-true}
Icon=$APP_ICON
Type=Application
EOF

cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG_NAME
Version: $PKG_VERSION
Architecture: arm64
Maintainer: $PKG_MAINTAINER
Section: APPLaunch
Priority: optional
Homepage: $PKG_HOMEPAGE
Depends: $PKG_DEPENDS
Description: $PKG_DESC
EOF

OUT="/tmp/${PKG_NAME}_${PKG_VERSION}_arm64.deb"
if dpkg-deb --help 2>/dev/null | grep -q -- '--root-owner-group'; then
  dpkg-deb --root-owner-group -b "$STAGE" "$OUT"
else
  dpkg-deb -b "$STAGE" "$OUT"
fi

echo "Installing $OUT..."
sudo dpkg -i "$OUT"

echo
echo "Installed Ghost Hunter."
echo "Open it from the launcher. Log out and back in once so the spi, gpio, and i2c groups apply."
echo "If the icon is missing: systemctl --user restart APPLaunch"

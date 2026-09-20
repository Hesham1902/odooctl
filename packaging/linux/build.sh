#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
output_dir="${1:-$repo_root/dist}"
build_root="$repo_root/build/linux"
app_dir="$build_root/AppDir"
deb_dir="$build_root/deb"

cd "$repo_root"

appimagetool="${APPIMAGETOOL:-}"
if [[ -z "$appimagetool" || ! -x "$appimagetool" ]]; then
  echo "APPIMAGETOOL must point to an executable appimagetool binary." >&2
  exit 1
fi
appimagetool="$(cd "$(dirname "$appimagetool")" && pwd)/$(basename "$appimagetool")"

version="$(python -c 'from odooctl import __version__; print(__version__)')"
rm -rf "$build_root" "$output_dir"
mkdir -p "$output_dir"

python -m PyInstaller \
  --noconfirm \
  --clean \
  --onefile \
  --windowed \
  --name odooctl-gui \
  --paths src \
  --hidden-import PySide6.QtCore \
  --hidden-import PySide6.QtGui \
  --hidden-import PySide6.QtWidgets \
  --distpath "$build_root/dist" \
  --workpath "$build_root/work" \
  --specpath "$build_root" \
  src/odooctl/gui_app.py

mkdir -p \
  "$app_dir/usr/bin" \
  "$app_dir/usr/share/applications" \
  "$app_dir/usr/share/icons/hicolor/scalable/apps"
install -Dm755 "$build_root/dist/odooctl-gui" "$app_dir/usr/bin/odooctl-gui"
install -Dm644 packaging/linux/odooctl.desktop "$app_dir/odooctl.desktop"
install -Dm644 packaging/linux/odooctl.svg "$app_dir/odooctl.svg"
install -Dm644 packaging/linux/odooctl.desktop "$app_dir/usr/share/applications/odooctl.desktop"
install -Dm644 packaging/linux/odooctl.svg \
  "$app_dir/usr/share/icons/hicolor/scalable/apps/odooctl.svg"

cat > "$app_dir/AppRun" <<'EOF'
#!/usr/bin/env sh
exec "$APPDIR/usr/bin/odooctl-gui" "$@"
EOF
chmod +x "$app_dir/AppRun"

APPIMAGE_EXTRACT_AND_RUN=1 "$appimagetool" "$app_dir" \
  "$output_dir/odooctl-linux-x86_64.AppImage"

mkdir -p \
  "$deb_dir/DEBIAN" \
  "$deb_dir/usr/bin" \
  "$deb_dir/usr/share/applications" \
  "$deb_dir/usr/share/icons/hicolor/scalable/apps"
install -Dm755 "$build_root/dist/odooctl-gui" "$deb_dir/usr/bin/odooctl-gui"
install -Dm644 packaging/linux/odooctl.desktop "$deb_dir/usr/share/applications/odooctl.desktop"
install -Dm644 packaging/linux/odooctl.svg \
  "$deb_dir/usr/share/icons/hicolor/scalable/apps/odooctl.svg"
cat > "$deb_dir/DEBIAN/control" <<EOF
Package: odooctl-gui
Version: $version
Section: devel
Priority: optional
Architecture: amd64
Maintainer: Hesham Mahmoud <hesham@users.noreply.github.com>
Depends: libc6 (>= 2.35), libdbus-1-3, libegl1, libfontconfig1, libfreetype6, libgl1, libx11-6, libx11-xcb1, libxcb-cursor0, libxcb-icccm4, libxcb-image0, libxcb-keysyms1, libxcb-randr0, libxcb-render-util0, libxcb-render0, libxcb-shape0, libxcb-shm0, libxcb-sync1, libxcb-xfixes0, libxcb-xkb1, libxkbcommon0, libxkbcommon-x11-0
Description: Desktop project manager for local Odoo Docker environments
 A free desktop interface for discovering, starting, stopping, and inspecting
 local Odoo Docker Compose projects.
EOF

dpkg-deb --build --root-owner-group "$deb_dir" \
  "$output_dir/odooctl-linux-x86_64.deb" >/dev/null

sha256sum "$output_dir/odooctl-linux-x86_64.AppImage" \
  > "$output_dir/odooctl-linux-x86_64.AppImage.sha256"
sha256sum "$output_dir/odooctl-linux-x86_64.deb" \
  > "$output_dir/odooctl-linux-x86_64.deb.sha256"

echo "Built Linux desktop packages for odooctl $version in $output_dir"

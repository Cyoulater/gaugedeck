#!/bin/sh
# Builds the download files for a release into dist/:
#   gaugedeck.deb     Ubuntu, Debian, Linux Mint, Pop!_OS, Zorin
#   gaugedeck.rpm     Fedora (needs rpmbuild)
#   gaugedeck.tar.gz  everything else (unpack, then: sh install.sh)
# Names carry no version so "releases/latest/download/<name>" links never change.
set -e
cd "$(dirname "$0")/.."
VERSION="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' gaugedeck.py)"
[ -n "$VERSION" ] || { echo "VERSION not found in gaugedeck.py" >&2; exit 1; }
ID=io.github.cyoulater.GaugeDeck
rm -rf dist build && mkdir -p dist build

# lay out the system-wide install tree shared by .deb and .rpm
stage() {
    install -Dm755 gaugedeck.py "$1/usr/bin/gaugedeck"
    install -Dm644 $ID.desktop "$1/usr/share/applications/$ID.desktop"
    install -Dm644 $ID.svg "$1/usr/share/icons/hicolor/scalable/apps/$ID.svg"
    install -Dm644 $ID.metainfo.xml "$1/usr/share/metainfo/$ID.metainfo.xml"
    install -Dm644 LICENSE "$1/$2"
}

# .deb
D=build/deb
stage $D usr/share/doc/gaugedeck/copyright
mkdir -p $D/DEBIAN
cat > $D/DEBIAN/control <<CTL
Package: gaugedeck
Version: $VERSION
Architecture: all
Maintainer: Miguel Ycaza <https://github.com/Cyoulater/gaugedeck>
Depends: python3, python3-gi, python3-gi-cairo, gir1.2-gtk-4.0
Recommends: lm-sensors
Section: utils
Priority: optional
Homepage: https://github.com/Cyoulater/gaugedeck
Description: Car-style gauges for your PC's temps, fans and load
 GaugeDeck shows CPU and GPU temperature, load, fan speeds, RAM use and
 board temperature as a dashboard of car-style dials, with alerts.
CTL
dpkg-deb --root-owner-group --build $D dist/gaugedeck.deb

# .rpm
if command -v rpmbuild >/dev/null; then
    R="$PWD/build/rpm"
    stage "$R/root" usr/share/licenses/gaugedeck/LICENSE
    cat > build/gaugedeck.spec <<SPEC
Name: gaugedeck
Version: $VERSION
Release: 1
Summary: Car-style gauges for your PC's temps, fans and load
License: GPL-3.0-or-later
URL: https://github.com/Cyoulater/gaugedeck
BuildArch: noarch
Requires: python3, python3-gobject, python3-cairo, gobject-introspection, gtk4
Recommends: lm_sensors
%description
GaugeDeck shows CPU and GPU temperature, load, fan speeds, RAM use and
board temperature as a dashboard of car-style dials, with alerts.
%install
cp -a $R/root/. %{buildroot}/
%files
/usr/bin/gaugedeck
/usr/share/applications/$ID.desktop
/usr/share/icons/hicolor/scalable/apps/$ID.svg
/usr/share/metainfo/$ID.metainfo.xml
%license /usr/share/licenses/gaugedeck/LICENSE
SPEC
    rpmbuild -bb --define "_topdir $R/top" --define "_build_id_links none" build/gaugedeck.spec
    cp "$R"/top/RPMS/noarch/gaugedeck-*.rpm dist/gaugedeck.rpm
else
    echo "rpmbuild not found, skipping .rpm"
fi

# .tar.gz with the per-user installer
T=build/gaugedeck
mkdir -p $T
cp gaugedeck.py install.sh uninstall.sh $ID.desktop $ID.svg $ID.metainfo.xml LICENSE README.md $T/
tar -C build -czf dist/gaugedeck.tar.gz gaugedeck

echo "Built GaugeDeck $VERSION:" && ls -l dist

#!/bin/sh
# Build distro packages in a throwaway container (needs docker or podman):
#   packaging/container-build.sh fedora|debian|ubuntu|arch [OUT_DIR]
# Nothing is installed on the host. Packages land in OUT_DIR (default dist/<distro>).
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DISTRO=${1:?usage: container-build.sh fedora|debian|ubuntu|arch [OUT_DIR]}
OUT=$(mkdir -p "${2:-$ROOT/dist/$DISTRO}" && cd "${2:-$ROOT/dist/$DISTRO}" && pwd)
ENGINE=$(command -v podman || command -v docker) || { echo "need podman or docker" >&2; exit 1; }
WORK=$(mktemp -d)
NAME="openhello-build-$DISTRO-$$"
# Also remove the container if we're interrupted (Ctrl+C, timeout): killing
# this script alone would leave it running.
trap '"$ENGINE" rm -f "$NAME" >/dev/null 2>&1; rm -rf "$WORK"' EXIT
trap 'exit 130' INT TERM

"$ROOT/packaging/make-tarball.sh" "$WORK"
VERSION=$(sed -n 's/^__version__ = "\([0-9.]*\)"/\1/p' "$ROOT/src/openhello/__init__.py")

# The SCRIPTs below are single-quoted on purpose: they expand inside the container.
# shellcheck disable=SC2016
case $DISTRO in
fedora)
    IMAGE=fedora:44
    SCRIPT='
      dnf -y install rpm-build dnf-plugins-core
      mkdir -p ~/rpmbuild/SOURCES && cp /work/*.tar.gz ~/rpmbuild/SOURCES/
      tar -xzf /work/*.tar.gz -C /tmp && SPEC=$(ls /tmp/openhello-*/packaging/fedora/openhello.spec)
      dnf -y builddep "$SPEC"
      for i in 1 2 3 4 5; do   # %generate_buildrequires, as mock does
        rm -f ~/rpmbuild/SRPMS/*buildreqs.nosrc.rpm
        rpmbuild -br "$SPEC" >/tmp/br.log 2>&1 && break
        dnf -y builddep ~/rpmbuild/SRPMS/*buildreqs.nosrc.rpm
      done
      rpmbuild -bb "$SPEC" >/tmp/build.log 2>&1 || { tail -40 /tmp/build.log; exit 1; }
      cp ~/rpmbuild/RPMS/*/*.rpm /out/' ;;
debian|ubuntu)
    [ "$DISTRO" = debian ] && IMAGE=debian:trixie || IMAGE=ubuntu:24.04
    SCRIPT='
      export DEBIAN_FRONTEND=noninteractive
      apt-get update && apt-get install -y --no-install-recommends devscripts equivs
      tar -xzf /work/*.tar.gz -C /tmp && cd /tmp/openhello-*
      cp -r packaging/debian debian
      mk-build-deps -i -r -t "apt-get -y --no-install-recommends" debian/control
      dpkg-buildpackage -b -us -uc >/tmp/build.log 2>&1 || { tail -40 /tmp/build.log; exit 1; }
      cp ../*.deb /out/' ;;
arch)
    IMAGE=archlinux:latest
    SCRIPT='
      pacman -Syu --noconfirm --needed base-devel
      useradd -m builder && echo "builder ALL=(ALL) NOPASSWD: ALL" >/etc/sudoers.d/builder
      mkdir /build && cp /work/*.tar.gz /build/ && tar -xzf /work/*.tar.gz -C /tmp
      cp /tmp/openhello-*/packaging/arch/PKGBUILD /build/ && chown -R builder /build
      cd /build && sudo -u builder makepkg --syncdeps --noconfirm >/tmp/build.log 2>&1 \
        || { tail -40 /tmp/build.log; exit 1; }
      cp /build/*.pkg.tar.zst /out/' ;;
*) echo "unknown distro $DISTRO" >&2; exit 1 ;;
esac

"$ENGINE" run --rm --name "$NAME" -v "$WORK:/work:ro,Z" -v "$OUT:/out:Z" "$IMAGE" sh -euc "$SCRIPT"
echo "openhello $VERSION ($DISTRO) -> $OUT:"
ls -1 "$OUT"

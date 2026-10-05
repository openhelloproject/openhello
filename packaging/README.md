# Packaging

| Target | Status |
|---|---|
| Fedora (`fedora/openhello.spec`) | **Supported.** Published via COPR. |
| Debian / Ubuntu (`debian/`) | Experimental, not maintained yet |
| Arch Linux (`arch/PKGBUILD`) | Experimental, not maintained yet |

## Release tarball

```bash
packaging/make-tarball.sh dist/
```

Produces `openhello-<version>.tar.gz` from the working tree, including the
bundled face models (YuNet, MIT; SFace, Apache-2.0), which are downloaded
and verified against pinned SHA-256 sums; see `models-LICENSES.md`.

## Fedora

Build the RPMs locally:

```bash
mkdir -p ~/rpmbuild/SOURCES && packaging/make-tarball.sh ~/rpmbuild/SOURCES
rpmbuild -bb packaging/fedora/openhello.spec
```

Or in a clean container (podman or docker), with nothing installed on the
host:

```bash
packaging/container-build.sh fedora dist/fedora
```

### COPR

The repository builds on COPR through `.copr/Makefile`:

1. The project is <https://copr.fedorainfracloud.org/coprs/penealfa/openhello/>
   (chroots `fedora-43-x86_64`, `fedora-44-x86_64` and `fedora-rawhide-x86_64`, "Follow Fedora
   branching" on, "Enable internet access during builds" off, AppStream
   metadata on).
2. Add a package: source type **SCM**, clone URL
   `https://github.com/openhelloproject/openhello`, spec
   `packaging/fedora/openhello.spec`, SRPM build method **make srpm**.
3. Optionally, enable the GitHub webhook to rebuild on every tag.

Users then install with:

```bash
sudo dnf copr enable penealfa/openhello
sudo dnf install openhello-setup
```

### Releasing

1. Update `__version__` in `src/openhello/__init__.py`, `Version:` and
   `%changelog` in the spec, the `<release>` in
   `data/org.openhello.Setup.metainfo.xml`, and `CHANGELOG.md`.
2. Run the tests and a container build.
3. Tag `vX.Y.Z` and trigger the COPR build.

# COPR "make srpm" entry point: https://docs.pagure.org/copr.copr/user_documentation.html#make-srpm
# COPR runs `make -f .copr/Makefile srpm outdir=... spec=...` with network
# access; the release tarball (including the face models, fetched and
# checksum-verified by make-tarball.sh) is built here, so the RPM build
# itself needs no network.

outdir ?= $(CURDIR)/dist/srpm
spec   := packaging/fedora/openhello.spec

srpm:
	dnf -y install curl tar rpm-build
	packaging/make-tarball.sh "$(outdir)"
	rpmbuild -bs \
	    --define "_sourcedir $(outdir)" \
	    --define "_srcrpmdir $(outdir)" \
	    "$(spec)"

.PHONY: srpm

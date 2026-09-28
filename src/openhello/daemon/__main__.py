"""
openhellod entry point: python3 -m openhello.daemon

Production: runs as root on the system bus (D-Bus activated, see data/).
Development: --bus session --tcti swtpm:port=2321 runs unprivileged against a
software TPM; see docs/TESTING.md.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from gi.repository import Gio, GLib

from ..backends.face import get_face_modality
from ..backends.fingerprint import FingerprintModality
from ..core.gcompat import unix_signal_add
from ..tpm.keystore import TPMKeystore
from .orchestrator import Orchestrator
from .service import BUS_NAME, DaemonService

log = logging.getLogger("openhellod")


def build_orchestrator(*, dev: bool, tcti: str | None) -> Orchestrator:
    modalities = {
        "fingerprint": FingerprintModality(),
        "face": get_face_modality(),
    }
    return Orchestrator(TPMKeystore(dev_mode=dev, tcti=tcti), modalities)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openhellod")
    parser.add_argument("--bus", choices=("system", "session"), default="system",
                        help="bus to serve on (session: unprivileged development)")
    parser.add_argument("--tcti", default=None,
                        help="TSS2 TCTI, e.g. swtpm:port=2321 (default: /dev/tpmrm0)")
    parser.add_argument("--dev", action="store_true",
                        help="INSECURE plaintext secret storage, for machines without any TPM")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    orchestrator = build_orchestrator(dev=args.dev, tcti=args.tcti)
    bus_type = Gio.BusType.SYSTEM if args.bus == "system" else Gio.BusType.SESSION
    conn = Gio.bus_get_sync(bus_type, None)
    service = DaemonService(orchestrator, conn)
    service.register()

    loop = GLib.MainLoop()
    exit_code = 0

    def on_name_lost(*_):
        nonlocal exit_code
        log.error("could not own %s on the %s bus (already running, or bus policy "
                  "not installed?)", BUS_NAME, args.bus)
        exit_code = 1
        loop.quit()

    Gio.bus_own_name_on_connection(
        conn, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
        lambda *_: log.info("serving %s on the %s bus (tcti=%s, dev=%s)",
                            BUS_NAME, args.bus, args.tcti, args.dev),
        on_name_lost)

    def on_signal(*_) -> bool:
        loop.quit()
        return GLib.SOURCE_REMOVE

    for sig in (signal.SIGINT, signal.SIGTERM):
        unix_signal_add(sig, on_signal)
    loop.run()
    service.unregister()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

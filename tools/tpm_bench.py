#!/usr/bin/env python3
"""
Time each step of an OpenHello unseal on the real TPM, to find where the
sign-in latency goes. Uses a throwaway state dir: it never reads or touches
/var/lib/openhello. Needs access to /dev/tpmrm0 (root, or the tss group):

  sudo python3 tools/tpm_bench.py [--rounds 5] [--tcti device:/dev/tpmrm0]

Prints the median of each step over the rounds (the first round is shown
separately: it includes loading the sealed object into the TPM).
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@contextmanager
def timed(results: dict[str, float], name: str):
    start = time.perf_counter()
    yield
    results[name] = time.perf_counter() - start


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--rounds", type=int, default=5)
    p.add_argument("--tcti", default=None)
    args = p.parse_args()

    tmp = tempfile.mkdtemp(prefix="openhello-tpmbench-")
    os.environ["OPENHELLO_STATE_DIR"] = tmp   # before importing openhello

    from openhello.tpm.keystore import TPMKeystore

    ks = TPMKeystore(tcti=args.tcti)
    cred = ks.generate_and_seal("bench")

    rows: list[dict[str, float]] = []
    for _ in range(args.rounds + 1):
        t: dict[str, float] = {}
        # How sign-in uses it: prepare while the user reaches the sensor,
        # finish after the match. Only "finish" delays the user.
        with timed(t, "prepare (during the scan)"):
            prepared = ks.prepare_unseal(cred)
        with timed(t, "finish (after the match)"):
            ks.finish_unseal(prepared)
        with timed(t, "prepare+abort (no match)"):
            ks.abort_unseal(ks.prepare_unseal(cred))
        rows.append(t)

    ks.close()
    first, rest = rows[0], rows[1:]
    print(f"{'step':28s} {'1st round':>10s} {'median':>10s}")
    for name in first:
        med = statistics.median(r[name] for r in rest)
        print(f"{name:28s} {first[name] * 1000:8.0f}ms {med * 1000:8.0f}ms")
    print(f"\n(throwaway state in {tmp}; safe to delete)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

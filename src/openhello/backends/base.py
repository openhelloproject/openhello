"""
The contract every biometric modality implements.

The daemon (daemon/orchestrator.py) only ever talks to modalities through
this interface, so adding a sensor type never touches the TPM, PAM or
D-Bus code. Long operations report progress through a callback and can be
cancelled cooperatively; both are called from the daemon's worker thread.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


class ModalityError(RuntimeError):
    """Operational failure: device missing/busy/disconnected. Distinct from a
    clean non-match, which verify() reports by returning False."""


class EnrollRejected(Exception):
    """The sensor worked but refused this enrollment for a reason the user
    can act on. `code` is stable (e.g. "duplicate"), message is English."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Cancelled(Exception):
    """Raised inside a modality when its CancelToken fires."""


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled()


@dataclass(frozen=True)
class Progress:
    """One enrollment/verification progress update, surfaced to the GUI.

    `status` is a stable machine-readable token (e.g. "enroll-stage-passed",
    "face-sample", "retry-scan"); `message` is human-readable English the GUI
    may show or replace with its own translated text.
    """
    status: str
    done: int = 0
    total: int = 0
    message: str = ""


ProgressCallback = Callable[[Progress], None]


def _no_progress(_: Progress) -> None:
    pass


class Modality(ABC):
    #: stable identifier used in the D-Bus API and on disk ("fingerprint", "face")
    name: str = ""

    @abstractmethod
    def is_available(self) -> bool:
        """Required hardware is present and usable right now."""

    @abstractmethod
    def is_enrolled(self, user: str) -> bool:
        """The user has biometric data this modality can verify against."""

    @abstractmethod
    def enroll(self, user: str, *, progress: ProgressCallback = _no_progress,
               cancel: CancelToken | None = None,
               options: Mapping[str, Any] | None = None) -> bool:
        """Capture/store biometric data. True on success, False on a clean
        failure (e.g. not enough usable samples). Raises ModalityError,
        Cancelled or NotImplementedError."""

    @property
    def can_authenticate(self) -> bool:
        """Whether verify() can ever succeed on this build. False for a
        modality that can enroll but isn't trusted for sign-in yet (IR face
        until liveness is calibrated); it is then skipped entirely during
        sign-in instead of scanning for nothing."""
        return True

    @abstractmethod
    def verify(self, user: str, *, cancel: CancelToken | None = None) -> bool:
        """Live match. True on match, False on clean no-match/timeout.
        Raises ModalityError, Cancelled or NotImplementedError."""

    def remove(self, user: str) -> None:  # noqa: B027 — optional hook, no-op default
        """Delete OpenHello-owned biometric data for `user` (default: none)."""

    def enrolled_items(self, user: str) -> list[str]:
        """Sub-items the user has enrolled, e.g. fingerprint finger names.
        Default: none."""
        return []

    def security_note(self) -> str:
        """Honest one-paragraph description of spoof resistance, shown in the
        GUI. See docs/SECURITY.md."""
        return "No liveness/anti-spoof guarantee implemented."

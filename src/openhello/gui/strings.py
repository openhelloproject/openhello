"""User-facing text in one place (the translation surface). Maps daemon status
tokens and error codes to plain language."""

from __future__ import annotations

from gettext import gettext as _

FINGER_LABELS = {
    "left-thumb": _("Left thumb"),
    "left-index-finger": _("Left index finger"),
    "left-middle-finger": _("Left middle finger"),
    "left-ring-finger": _("Left ring finger"),
    "left-little-finger": _("Left little finger"),
    "right-thumb": _("Right thumb"),
    "right-index-finger": _("Right index finger"),
    "right-middle-finger": _("Right middle finger"),
    "right-ring-finger": _("Right ring finger"),
    "right-little-finger": _("Right little finger"),
}

# Short labels for the finger picker buttons (the row already names the hand).
FINGER_SHORT = {
    "thumb": _("Thumb"), "index-finger": _("Index"), "middle-finger": _("Middle"),
    "ring-finger": _("Ring"), "little-finger": _("Little"),
}


def finger_short(finger: str) -> str:
    return FINGER_SHORT.get(finger.split("-", 1)[1], finger)


HANDS = (
    (_("Left hand"), ("left-thumb", "left-index-finger", "left-middle-finger",
                      "left-ring-finger", "left-little-finger")),
    (_("Right hand"), ("right-thumb", "right-index-finger", "right-middle-finger",
                       "right-ring-finger", "right-little-finger")),
)

# fprintd EnrollStatus results -> instruction shown under the animation
FINGERPRINT_STATUS = {
    "enroll-stage-passed": _("Great — lift your finger and place it again"),
    "enroll-retry-scan": _("Didn't catch that — try again"),
    "enroll-swipe-too-short": _("Swipe a little more slowly"),
    "enroll-finger-not-centered": _("Center your finger on the sensor"),
    "enroll-remove-and-retry": _("Lift your finger, then place it again"),
    "enroll-completed": _("Done!"),
}
RETRY_STATUSES = {"enroll-retry-scan", "enroll-swipe-too-short",
                  "enroll-finger-not-centered", "enroll-remove-and-retry"}

FACE_STATUS = {
    "face-searching": _("Look at the screen"),
    "face-sample": _("Hold still… now turn your head slightly"),
    "face-timeout": _("Couldn't see your face clearly"),
}

ERRORS = {
    "PermissionDenied": _("Authentication was cancelled or refused."),
    "Busy": _("Another enrollment is already running."),
    "DeviceError": _("The sensor is busy or disconnected. If the screen is locked in "
                     "another session, unlock it and try again."),
    "NotAvailable": _("This sensor isn't available on this device."),
    "EnrollFailed": _("Enrollment didn't finish. Please try again."),
    "Duplicate": _("This fingerprint is already registered — maybe under a different "
                   "finger name. Try a finger you haven't added yet."),
    "Cancelled": _("Enrollment was cancelled."),
    "NotImplemented": _("This isn't supported on this device yet."),
    "ServiceUnavailable": _("The OpenHello service isn't installed or running."),
}


def error_text(code: str, fallback: str = "") -> str:
    return ERRORS.get(code, fallback or _("Something went wrong."))

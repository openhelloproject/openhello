"""Username validation, shared by the daemon and mirrored in pam/pam_openhello.c."""

from __future__ import annotations

import re

# Conservative POSIX-portable username. Also keeps a name safe to use as a
# path component under the state dir. pam_openhello.c: valid_username().
_USERNAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}")


class InvalidUsername(ValueError):
    pass


def is_valid_username(user: object) -> bool:
    return isinstance(user, str) and _USERNAME_RE.fullmatch(user) is not None


def validate_username(user: object) -> str:
    if not is_valid_username(user):
        raise InvalidUsername(f"invalid username {user!r}")
    return user  # type: ignore[return-value]

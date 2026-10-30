"""Signal a process group the suite started."""

from __future__ import annotations

import os


def signal_group(group: int, signum: int) -> bool:
    """Send `signum` to every process in `group`; False when the group has
    nothing left to stop.

    That is the outcome a stop wants, so it is an answer rather than an error.
    Linux gives it as ESRCH. Darwin also gives EPERM, for a group whose members
    are all zombies: the caller's own leader once it exits unreaped, or orphans
    their new parent has not reaped yet. Linux takes the signal for such a group
    instead, so True promises only that the kernel took it. EPERM means no
    member could be signalled, and a stop here signals only a group it started,
    under its own user, so that too is nothing left to stop. Any other OSError
    raises.
    """
    try:
        os.killpg(group, signum)
    except ProcessLookupError, PermissionError:
        return False
    return True

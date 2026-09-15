#!/usr/bin/env python3
"""macOS Local Network privacy: recognise a denial and trigger the permission prompt.

macOS 15+ gates LAN traffic per process ("Local Network" under System Settings >
Privacy & Security). A denied process gets EHOSTUNREACH ("No route to host") on
connect()/sendto() to any LAN address — indistinguishable from a routing
problem, except that the kernel log says ``reason: NECP``.

Two things make this a trap for a command-line player:

- The permission is judged against the *responsible process*. Terminals such as
  iTerm2 disclaim responsibility for everything they spawn, so their own grant
  does not cover us and the decision falls to Python itself.
- macOS only shows the permission prompt when the responsible process is a
  promptable app. In the disclaimed-by-a-terminal chain it never asks — it just
  denies.

``request_access()`` gets around that by re-running our probe with Python as its
own responsible process (``responsibility_spawnattrs_setdisclaim``, the same
call iTerm2 uses), which is exactly the condition under which macOS does prompt.
"""

import errno
import ipaddress
import logging
import os
import socket
import sys

from ..protocol import slimproto

log = logging.getLogger("squeezy")

HINT = (
    'Hint: on macOS, "No route to host" to a LAN address usually means Local Network '
    "access is denied for this process (System Settings > Privacy & Security > Local "
    "Network). Terminals such as iTerm2 don't pass their permission on to the programs "
    "they run, and macOS won't prompt for it — run `squeezy --request-local-network` "
    "to trigger the prompt."
)

# Exit codes of the re-spawned probe (also what request_access() returns).
PROBE_ALLOWED = 0
PROBE_DENIED = 1
PROBE_UNKNOWN = 2


def hint_for(err, host):
    """Return HINT if ``err`` looks like a macOS Local Network denial, else None.

    Args:
        err: The OSError raised by connect()/sendto().
        host: The address we were trying to reach.
    """
    if sys.platform != "darwin" or err.errno != errno.EHOSTUNREACH:
        return None
    try:
        if ipaddress.ip_address(host).is_global:
            return None  # a real routing failure to the internet
    except ValueError:
        pass  # hostname — can't classify; a LAN name is the common case
    return HINT


def probe(server, port=slimproto.SLIMPROTO_PORT):
    """Try to reach LMS and classify the outcome (runs in the re-spawned child).

    Returns:
        PROBE_ALLOWED if we reached a server, PROBE_DENIED on EHOSTUNREACH,
        PROBE_UNKNOWN if nothing answered (says nothing about the permission).
    """
    from .server_connection import ServerConnection  # local: it imports us for hint_for()
    if server is None:
        # Discovery swallows sendto errors, so a denial shows up as "nothing found".
        return PROBE_ALLOWED if ServerConnection.discover_lms(port) else PROBE_UNKNOWN
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(slimproto.CONNECT_TIMEOUT_SEC)
    try:
        sock.connect((server, port))
        return PROBE_ALLOWED
    except OSError as e:
        return PROBE_DENIED if e.errno == errno.EHOSTUNREACH else PROBE_UNKNOWN
    finally:
        sock.close()


def spawn_disclaimed(argv):
    """Run ``argv`` as its own responsible process (macOS) and return its exit code.

    posix_spawn with ``responsibility_spawnattrs_setdisclaim`` — private but
    stable since 10.14 and used by iTerm2, Qt and friends. Python's own
    os.posix_spawn cannot set that attribute, hence ctypes.
    """
    import ctypes
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    libq = ctypes.CDLL("/usr/lib/system/libquarantine.dylib")
    libq.responsibility_spawnattrs_setdisclaim.argtypes = [ctypes.c_void_p, ctypes.c_int]
    libq.responsibility_spawnattrs_setdisclaim.restype = ctypes.c_int

    attr = ctypes.c_void_p()  # posix_spawnattr_t is an opaque pointer on macOS
    rc = libc.posix_spawnattr_init(ctypes.byref(attr))
    if rc != 0:
        raise OSError(rc, "posix_spawnattr_init: " + os.strerror(rc))
    try:
        rc = libq.responsibility_spawnattrs_setdisclaim(ctypes.byref(attr), 1)
        if rc != 0:
            raise OSError(rc, "responsibility_spawnattrs_setdisclaim: " + os.strerror(rc))
        c_argv = (ctypes.c_char_p * (len(argv) + 1))(*[a.encode() for a in argv], None)
        env = [f"{k}={v}".encode() for k, v in os.environ.items()]
        c_env = (ctypes.c_char_p * (len(env) + 1))(*env, None)
        pid = ctypes.c_int()
        rc = libc.posix_spawn(ctypes.byref(pid), argv[0].encode(), None, ctypes.byref(attr), c_argv, c_env)
        if rc != 0:
            raise OSError(rc, "posix_spawn: " + os.strerror(rc))
    finally:
        libc.posix_spawnattr_destroy(ctypes.byref(attr))
    _, status = os.waitpid(pid.value, 0)
    return os.waitstatus_to_exitcode(status)


def request_access(server):
    """Trigger the macOS Local Network prompt for Python and report the outcome.

    Args:
        server: LMS IP from -s, or None to use discovery.

    Returns:
        Process exit code: PROBE_ALLOWED / PROBE_DENIED / PROBE_UNKNOWN.
    """
    if sys.platform != "darwin":
        print("Local Network permission is a macOS thing — nothing to do here.")
        return 0

    print("Checking Local Network access as Python itself.")
    print("If macOS asks whether Python may find devices on your local network, click Allow.")
    argv = [sys.executable, "-m", "squeezy", "--local-network-probe"]
    if server:
        argv += ["-s", server]
    result = spawn_disclaimed(argv)

    if result == PROBE_ALLOWED:
        print("Local Network access is allowed — squeezy can reach LMS.")
    elif result == PROBE_DENIED:
        print("Local Network access is denied for Python.")
        print("If a prompt just appeared, click Allow and run this again to confirm.")
        print("Otherwise open System Settings > Privacy & Security > Local Network and turn Python on.")
    else:
        print("No LMS answered, so this says nothing about the permission.")
        print("Run again with -s <LMS IP> for a definitive check.")
    return result

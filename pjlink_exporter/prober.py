"""
PJLink device prober.

Speaks the PJLink protocol (JBMIA standard, https://pjlink.jbmia.or.jp/) directly
over a raw TCP socket — no vendor SDK or MIB required. PJLink is supported by
Epson's business/education projector line and most other major projector
brands, so this client works generically across vendors instead of being tied
to one manufacturer's proprietary API.

Protocol basics (see the PJLink Class 1/2 specification for the full command
set):

- TCP port 4352. On connect, the device sends a one-line greeting:
  ``PJLINK 0`` (no authentication) or ``PJLINK 1 <8-char-seed>`` (password
  required). ``PJLINK ERRA`` means the device rejected the connection outright
  (e.g. too many failed auth attempts).
- When authentication is required, the *first* command sent on the connection
  is prefixed with ``md5(seed + password)`` as a 32-char lowercase hex string.
  Only the first command needs this prefix — the authenticated session then
  covers every subsequent command on that same TCP connection.
- Every command/response line is terminated with ``\\r`` (not ``\\n``).
- Commands are strictly request/response, one at a time — PJLink does not
  pipeline.
- Class 1 is power status, error status, lamp hours, and device info. Class 2
  (newer models) adds extras including filter usage hours (``FILT``). Not
  every device implements every command, even within its class, so each query
  is treated as best-effort: a device-side ``ERR1`` ("undefined command") for
  one field must not fail the whole probe.

A fresh TCP connection is opened per probe rather than pooling connections,
since PJLink servers are free to drop idle connections and Prometheus scrapes
are infrequent enough that connection reuse buys nothing.
"""

from __future__ import annotations

import hashlib
import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_PORT = 4352

_ENCODING = "utf-8"
_TERMINATOR = b"\r"
# A PJLink response line is a handful of bytes; cap the read so a
# misbehaving device (or something answering on the wrong port) can't make
# us buffer forever waiting for a terminator that never arrives.
_MAX_LINE_BYTES = 512

# ERST reports six components in a fixed order, each as a single digit:
# 0 = no error, 1 = warning, 2 = error.
_ERST_COMPONENTS = ("fan", "lamp", "temperature", "cover_open", "filter", "other")

_ERR_MESSAGES = {
    "ERR1": "undefined command",
    "ERR2": "out of parameter",
    "ERR3": "unavailable at this time",
    "ERR4": "projector/display failure",
}


@dataclass
class ProbeResult:
    """Result of probing a single PJLink device."""

    success: bool = False
    error: Optional[str] = None
    duration_seconds: float = 0.0

    pjlink_class: Optional[int] = None
    # Raw POWR code: 0=off, 1=on, 2=cooling, 3=warm-up.
    power_state: Optional[int] = None
    # component name -> 0/1/2, from ERST. Only populated for components the
    # device actually reported.
    errors: dict[str, int] = field(default_factory=dict)
    # (hours, is_on) per lamp, in device-reported order. Most projectors have
    # exactly one lamp; PJLink allows up to eight.
    lamps: list[tuple[int, bool]] = field(default_factory=list)
    # Class 2 only (FILT). None if the device is Class 1 or didn't answer.
    filter_hours: Optional[int] = None

    name: Optional[str] = None
    manufacturer: Optional[str] = None
    product_name: Optional[str] = None


class _ProtocolError(ConnectionError):
    """Malformed or unexpected data from the device (not a plain socket error)."""


class _PJLinkSession:
    """
    One PJLink TCP connection: handshake once, then issue a sequence of
    queries against it.
    """

    def __init__(self, sock: socket.socket, password: Optional[str]) -> None:
        self._sock = sock
        self._password = password
        self._buf = b""
        self._auth_prefix = ""
        self._first_command_sent = False

    def _recv_line(self) -> str:
        while _TERMINATOR not in self._buf:
            chunk = self._sock.recv(1024)
            if not chunk:
                raise _ProtocolError("connection closed by device")
            self._buf += chunk
            if len(self._buf) > _MAX_LINE_BYTES:
                raise _ProtocolError("response line exceeded max length")
        line, self._buf = self._buf.split(_TERMINATOR, 1)
        return line.decode(_ENCODING, errors="replace")

    def handshake(self) -> None:
        """Read the greeting and compute the auth prefix, if required."""
        greeting = self._recv_line()
        if greeting == "PJLINK ERRA":
            raise PermissionError("device rejected the connection (authentication error)")

        parts = greeting.split(" ")
        if len(parts) < 2 or parts[0] != "PJLINK":
            raise _ProtocolError(f"unexpected greeting: {greeting!r}")

        if parts[1] == "1":
            if len(parts) < 3:
                raise _ProtocolError(f"malformed auth greeting: {greeting!r}")
            if not self._password:
                raise PermissionError("device requires a password but none is configured")
            seed = parts[2]
            self._auth_prefix = hashlib.md5(
                (seed + self._password).encode(_ENCODING)
            ).hexdigest()
        elif parts[1] != "0":
            raise _ProtocolError(f"unexpected greeting: {greeting!r}")

    def query(self, cmd: str, klass: int = 1) -> str:
        """
        Send a class-1 or class-2 query and return its value.

        :raises LookupError: The device answered with a PJLink ``ERRx`` code
            (e.g. the command isn't implemented) — expected for optional
            commands, not a connection failure.
        """
        prefix = ""
        if not self._first_command_sent:
            prefix = self._auth_prefix
            self._first_command_sent = True

        self._sock.sendall(f"{prefix}%{klass}{cmd} ?\r".encode(_ENCODING))
        line = self._recv_line()

        expect = f"%{klass}{cmd}="
        if not line.startswith(expect):
            raise _ProtocolError(f"unexpected response to {cmd}: {line!r}")

        value = line[len(expect):]
        if value in _ERR_MESSAGES:
            raise LookupError(_ERR_MESSAGES[value])
        return value


def probe_device(
    host: str,
    port: int = DEFAULT_PORT,
    password: Optional[str] = None,
    timeout: float = 10.0,
) -> ProbeResult:
    """
    Query a PJLink device for power/error/lamp/filter status and device info.

    Connectivity and protocol failures (DNS, TCP, auth, unexpected framing)
    are caught and reported via :attr:`ProbeResult.success` /
    :attr:`ProbeResult.error` rather than raised, so callers can always emit a
    ``probe_success`` metric. Individual optional queries (``ERST``, ``LAMP``,
    ``FILT``, device-info) are independently best-effort: a device declining
    one of them with an ``ERRx`` code leaves that field ``None``/empty on the
    result without failing the whole probe. The probe as a whole is only
    considered successful once the mandatory ``POWR`` query succeeds, since
    that's the minimum needed to say the device is up and speaking PJLink.
    """
    start = time.monotonic()
    result = ProbeResult()

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            session = _PJLinkSession(sock, password)
            session.handshake()

            try:
                result.pjlink_class = int(session.query("CLSS"))
            except (LookupError, ValueError) as exc:
                # Not every Class 1 device implements CLSS itself; assume the
                # minimum class and skip class-2-only queries below.
                result.pjlink_class = 1
                logger.debug("CLSS query failed for %s, assuming class 1: %s", host, exc)

            result.power_state = int(session.query("POWR"))
            result.success = True

            try:
                erst = session.query("ERST")
                if len(erst) == len(_ERST_COMPONENTS):
                    result.errors = dict(zip(_ERST_COMPONENTS, (int(c) for c in erst)))
                else:
                    logger.debug("ERST response had unexpected length for %s: %r", host, erst)
            except (LookupError, ValueError) as exc:
                logger.debug("ERST query failed for %s: %s", host, exc)

            try:
                fields = session.query("LAMP").split(" ")
                result.lamps = [
                    (int(fields[i]), fields[i + 1] == "1")
                    for i in range(0, len(fields) - 1, 2)
                ]
            except (LookupError, ValueError, IndexError) as exc:
                logger.debug("LAMP query failed for %s: %s", host, exc)

            if result.pjlink_class and result.pjlink_class >= 2:
                try:
                    result.filter_hours = int(session.query("FILT", klass=2))
                except (LookupError, ValueError) as exc:
                    logger.debug("FILT query failed for %s: %s", host, exc)

            for attr, cmd in (("name", "NAME"), ("manufacturer", "INF1"), ("product_name", "INF2")):
                try:
                    setattr(result, attr, session.query(cmd))
                except LookupError as exc:
                    logger.debug("%s query failed for %s: %s", cmd, host, exc)

    except (OSError, _ProtocolError, PermissionError) as exc:
        result.error = str(exc)
        logger.warning("Probe failed for %s:%d: %s", host, port, exc)
    finally:
        result.duration_seconds = time.monotonic() - start

    return result

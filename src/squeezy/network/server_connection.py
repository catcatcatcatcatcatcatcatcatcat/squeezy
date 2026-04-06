#!/usr/bin/env python3
"""SlimProto server connection management (TCP/UDP discovery).

Handles low-level socket lifecycle: discovery, connect, disconnect, send, recv.
No protocol knowledge above SlimProto framing (4-byte opcode + u32 length).
"""

import logging
import socket
import threading

log = logging.getLogger("squeezy")


class ServerConnection:
    """TCP connection to LMS with UDP discovery fallback."""

    def __init__(self, port: int = 3483, timeout_sec: float = 1.0):
        """Initialize connection manager.

        Args:
            port: SlimProto port (default 3483)
            timeout_sec: Socket read timeout in seconds
        """
        self.port = port
        self.timeout_sec = timeout_sec
        self._sock = None
        self._send_lock = threading.Lock()

    @staticmethod
    def discover_lms(port: int = 3483) -> str:
        """Discover LMS via UDP broadcast.

        Sends "e" probe on port 3483 and listens for "E" response with server IP.
        Tries 255.255.255.255 plus interface-specific and common subnet broadcasts.

        Args:
            port: SlimProto UDP port

        Returns:
            Server IP address (string), or None if not found
        """
        log.info("Discovering server...")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(5)

        # Try multiple broadcast addresses (255.255.255.255 fails on some macOS configs)
        broadcast_addrs = ["255.255.255.255"]
        try:
            import netifaces
            for iface in netifaces.interfaces():
                addrs = netifaces.ifaddresses(iface).get(netifaces.AF_INET, [])
                for addr in addrs:
                    if "broadcast" in addr:
                        broadcast_addrs.append(addr["broadcast"])
        except ImportError:
            # Fallback: try common subnet broadcasts
            broadcast_addrs.extend(["192.168.1.255", "192.168.0.255", "10.0.0.255", "172.16.0.255"])

        for attempt in range(5):
            for bcast in broadcast_addrs:
                try:
                    sock.sendto(b"e", (bcast, port))
                except OSError:
                    continue
            try:
                data, addr = sock.recvfrom(1024)
                if data and data[0:1] == b"E":
                    log.info("Found server at %s", addr[0])
                    sock.close()
                    return addr[0]
            except socket.timeout:
                log.debug("Discovery attempt %d timed out", attempt + 1)
        sock.close()
        return None

    def connect(self, server_ip: str) -> bool:
        """Connect to LMS via TCP.

        Args:
            server_ip: Server IP address

        Returns:
            True on success, False on failure
        """
        try:
            log.debug("Connecting to %s:%d", server_ip, self.port)
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(self.timeout_sec)
            self._sock.connect((server_ip, self.port))
            log.info("Connected to %s", server_ip)
            return True
        except OSError as e:
            log.warning("Connection failed: %s", e)
            self._sock = None
            return False

    def disconnect(self) -> None:
        """Close TCP connection."""
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def is_connected(self) -> bool:
        """Check if TCP connection is active."""
        return self._sock is not None

    def send(self, data: bytes) -> None:
        """Send data to server (thread-safe).

        Args:
            data: Raw bytes to send (complete packet)
        """
        with self._send_lock:
            try:
                self._sock.sendall(data)
            except OSError as e:
                log.warning("Send error: %s", e)

    def recv(self, bufsize: int = 4096) -> bytes:
        """Receive data from server with timeout handling.

        Args:
            bufsize: Maximum bytes to read

        Returns:
            Bytes received, b"" if server closed, None on timeout

        Raises:
            OSError: On hard socket errors
        """
        try:
            return self._sock.recv(bufsize)
        except socket.timeout:
            return None

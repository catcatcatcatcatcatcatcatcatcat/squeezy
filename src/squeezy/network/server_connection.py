#!/usr/bin/env python3
"""SlimProto server connection management (TCP/UDP discovery).

Handles low-level socket lifecycle: discovery, connect, disconnect, send, recv.
No protocol knowledge above SlimProto framing (4-byte opcode + u32 length).
"""

import logging
import socket
import threading

from ..protocol import slimproto

log = logging.getLogger("squeezy")


class ServerConnection:
    """TCP connection to LMS with UDP discovery fallback.

    Manages the SlimProto TCP socket used for the main control channel.
    Also provides static UDP discovery to find LMS on the local network.
    """

    def __init__(self, port=slimproto.SLIMPROTO_PORT, timeout_sec=slimproto.RECV_TIMEOUT_SEC):
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
    def discover_lms(port=slimproto.SLIMPROTO_PORT):
        """Discover LMS on the local network via UDP broadcast.

        Sends a single-byte probe ('e') on the SlimProto port and listens
        for the LMS response ('E' prefix + server info). Tries multiple
        broadcast addresses because 255.255.255.255 fails on some macOS
        network configurations (bridged interfaces, VPNs).

        Args:
            port: SlimProto UDP port to probe

        Returns:
            Server IP address (string), or None if not found after all attempts
        """
        log.info("Discovering server...")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(slimproto.DISCOVERY_TIMEOUT_SEC)

        # Build list of broadcast addresses to try.
        # Start with the subnet-specific broadcast for our outbound interface,
        # which we detect by connecting a UDP socket (no packets sent).
        # This works on any subnet (10.x, 192.168.x, etc.) without netifaces.
        broadcast_addrs = []
        try:
            _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            _s.connect(("255.255.255.255", 1))
            local_ip = _s.getsockname()[0]
            _s.close()
            # Assume /24 — covers virtually all home/office LANs
            subnet_bcast = local_ip.rsplit(".", 1)[0] + ".255"
            broadcast_addrs.append(subnet_bcast)
            log.debug("Local IP %s → trying subnet broadcast %s", local_ip, subnet_bcast)
        except OSError:
            pass
        broadcast_addrs.append("255.255.255.255")
        try:
            import netifaces
            for iface in netifaces.interfaces():
                addrs = netifaces.ifaddresses(iface).get(netifaces.AF_INET, [])
                for addr in addrs:
                    if "broadcast" in addr:
                        broadcast_addrs.append(addr["broadcast"])
        except ImportError:
            pass  # subnet broadcast above already covers the common case

        for attempt in range(slimproto.DISCOVERY_ATTEMPTS):
            for bcast in broadcast_addrs:
                try:
                    sock.sendto(slimproto.UDP_DISCOVER_PROBE, (bcast, port))
                except OSError:
                    continue
            try:
                data, addr = sock.recvfrom(slimproto.DISCOVERY_RECV_SIZE)
                if data and data[0:1] == slimproto.UDP_DISCOVER_RESPONSE:
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

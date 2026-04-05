#!/usr/bin/env python3
"""LMS client for SlimProto message sending and codec detection.

High-level LMS protocol operations: HELO registration, STAT heartbeats,
codec probing via ffmpeg. Depends on server_connection for raw I/O.
"""

import logging
import re

from . import slimproto

log = logging.getLogger("squeezy")


class LmsClient:
    """SlimProto protocol client for sending messages to LMS."""

    def __init__(self, conn):
        """Initialize LMS client.

        Args:
            conn: ServerConnection instance for I/O
        """
        self.conn = conn

    def send_helo(self, mac: bytes, caps: str, reconnect: bool = False,
                  bytes_received: int = 0) -> None:
        """Send HELO packet to register player with LMS.

        Args:
            mac: 6-byte MAC address
            caps: Capabilities string (model, firmware, codecs, etc.)
            reconnect: bool — 0x4000 flag if True (reconnection), 0x0000 if False
            bytes_received: Lifetime bytes received counter
        """
        pkt = slimproto.build_helo(mac, caps, reconnect, bytes_received)
        self.conn.send(pkt)
        log.debug("Sent HELO packet with capabilities: %s", caps)

    def send_stat(self, event: str, stream_buf_size: int = 0, stream_buf_full: int = 0,
                  bytes_received: int = 0, output_buf_size: int = 0, output_buf_full: int = 0,
                  elapsed_ms: int = 0, server_timestamp: int = 0) -> None:
        """Send STAT (status) packet to report playback state.

        Args:
            event: 4-char event code (e.g. "STMt", "STMs", "STMd")
            stream_buf_size: Total stream buffer size
            stream_buf_full: Bytes in stream buffer
            bytes_received: Total bytes received from stream server
            output_buf_size: Total output (PCM) buffer size
            output_buf_full: Bytes in output buffer
            elapsed_ms: Milliseconds of audio played
            server_timestamp: Echo of jiffies from LMS 'strm t'
        """
        pkt = slimproto.build_stat(
            event,
            stream_buf_size=stream_buf_size,
            stream_buf_full=stream_buf_full,
            bytes_received=bytes_received,
            output_buf_size=output_buf_size,
            output_buf_full=output_buf_full,
            elapsed_ms=elapsed_ms,
            server_timestamp=server_timestamp,
        )
        self.conn.send(pkt)

    def send_dsco(self, reason: int = 0) -> None:
        """Send DSCO (disconnect) packet.

        Args:
            reason: Disconnect reason (0=ok, 1=local, 2=remote, 3=unreachable, 4=timeout)
        """
        pkt = slimproto.build_dsco(reason)
        self.conn.send(pkt)

    def send_resp(self, http_headers: bytes) -> None:
        """Send RESP (response) packet with HTTP headers.

        Args:
            http_headers: Raw HTTP response headers from stream server
        """
        pkt = slimproto.build_resp(http_headers)
        self.conn.send(pkt)

    def send_setd(self, player_id: int, data: bytes) -> None:
        """Send SETD (set data) packet.

        Args:
            player_id: Setting ID (0 = player name)
            data: Setting value as raw bytes
        """
        pkt = slimproto.build_setd(player_id, data)
        self.conn.send(pkt)

    @staticmethod
    def get_capabilities(version: str, name: str, codecs: list[str],
                        max_sample_rate: int = 192000) -> str:
        """Build SlimProto HELO capabilities string.

        Args:
            version: Player version string
            name: Player name
            codecs: List of supported codec short names (e.g., ['mp3', 'flac'])
            max_sample_rate: Maximum sample rate supported (Hz)

        Returns:
            Capabilities string for HELO packet (e.g. "Model=squeezeplay...")
        """
        codec_string = ",".join(codecs) if codecs else "pcm,mp3,flac"

        return (
            f"Model=squeezeplay"
            f"&ModelName=Squeezy"
            f"&AccuratePlayPoints=1"
            f"&HasDigitalOut=1"
            f"&HasPolarityInversion=1"
            f"&CanHTTPS=1"
            f"&Firmware={version}"
            f"&MaxSampleRate={max_sample_rate}"
            f"&Codecs={codec_string}"
        )

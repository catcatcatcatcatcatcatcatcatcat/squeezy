#!/usr/bin/env python3
"""SlimProto protocol constants, packet builders, and utility functions.

This module provides pure functions for SlimProto packet construction and
parsing, with zero dependency on the Squeezy class or player state.

Reference: https://wiki.slimdevices.com/index.php/SlimProto_TCP_protocol
"""

import struct
import time
import uuid

# ---------------------------------------------------------------------------
# SlimProto protocol constants
# ---------------------------------------------------------------------------

SLIMPROTO_PORT = 3483            # TCP/UDP port for SlimProto communication
DEVICE_ID = 12                   # SqueezePlay device type (we impersonate this)
STREAM_BUF_MAX = 2 * 1024 * 1024 # Stream buffer size reported to LMS (2 MB)

# ---------------------------------------------------------------------------
# Audio format constants
# ---------------------------------------------------------------------------

SAMPLE_RATE = 44100              # Default sample rate (Hz)
CHANNELS = 2                     # Stereo output
BYTES_PER_FRAME = 4              # 16-bit stereo = 2 bytes × 2 channels

# Supported sample rates — used to validate rates from LMS and ffmpeg.
# Rates outside this set fall back to 44100.
SUPPORTED_SAMPLE_RATES = [44100, 48000, 96000, 192000]

# ---------------------------------------------------------------------------
# Device buffer and latency constants
# ---------------------------------------------------------------------------

DEVICE_BUFFER_MSEC = 40          # miniaudio buffer size

# Platform-specific pipeline latency (OS audio stack depth below miniaudio).
# This is the extra delay between miniaudio handing audio to the OS and the
# user hearing it. Overridable at runtime via --latency CLI flag.
import sys as _sys
if _sys.platform == "darwin":
    PLATFORM_PIPELINE_MSEC = 40  # CoreAudio HAL buffer + IOAudio kernel stack
elif _sys.platform == "win32":
    PLATFORM_PIPELINE_MSEC = 30  # WASAPI shared mode
else:
    PLATFORM_PIPELINE_MSEC = 10  # Linux ALSA (thinner stack)

DEVICE_DELAY_MSEC = DEVICE_BUFFER_MSEC + PLATFORM_PIPELINE_MSEC

# ---------------------------------------------------------------------------
# Crossfade / transition type constants
#
# These match the values LMS sends in the strm 's' packet at offset 14.
# They control how tracks blend at boundaries.
# ---------------------------------------------------------------------------

FADE_NONE = 0       # Immediate switch, no fade
FADE_CROSSFADE = 1  # Old fades out while new fades in
FADE_IN = 2         # New track fades in from silence
FADE_OUT = 3        # Old track fades out to silence
FADE_INOUT = 4      # Both fade simultaneously

# ---------------------------------------------------------------------------
# Network timeouts and thresholds
# ---------------------------------------------------------------------------

CONNECT_TIMEOUT_SEC = 5          # TCP connect timeout for SlimProto
RECV_TIMEOUT_SEC = 1             # Socket recv timeout in message loop
SERVER_TIMEOUT_SEC = 35          # No-data threshold before reconnect
                                 # (LMS sends strm-t every ~5s; 35s accommodates
                                 # mysqueezebox.com which can go silent for ~30s)
STREAM_CONNECT_TIMEOUT_SEC = 10  # HTTP stream connection timeout
STREAM_READ_TIMEOUT_SEC = 5     # HTTP stream recv timeout during playback
DISCOVERY_ATTEMPTS = 5           # UDP broadcast discovery retry count
DISCOVERY_RECV_SIZE = 1024       # UDP discovery response buffer size
DISCOVERY_TIMEOUT_SEC = 5        # UDP discovery socket timeout
RECONNECT_DELAY_SEC = 2         # Delay before reconnecting after disconnect
RETRY_DELAY_SEC = 5             # Delay before retrying failed connection
FAILED_CONNECT_THRESHOLD = 5    # Consecutive failures before falling back to UDP

# UDP discovery magic bytes (cf. squeezelite slimproto.c discovery)
UDP_DISCOVER_PROBE = b"e"       # We send this to find LMS
UDP_DISCOVER_RESPONSE = b"E"    # LMS replies with this prefix

# HTTPS detection
HTTPS_PORT = 443                 # Port that triggers SSL wrapping

# ---------------------------------------------------------------------------
# Streaming buffer constants
# ---------------------------------------------------------------------------

HTTP_HEADER_RECV_SIZE = 4096     # Recv size for reading HTTP response headers
STREAM_RECV_SIZE = 32768         # Recv size for streaming audio data (32 KB)
FFMPEG_READ_SIZE = 8192          # Chunk size for reading ffmpeg stdout (8 KB)
MIN_THRESHOLD_BYTES = 8192      # Minimum buffer before playback starts (one chunk)
PCM_BUF_MAX_SIZE = 4 * 1024 * 1024  # Max PCM buffer size (4 MB, ~23s at 44.1k stereo)

# ---------------------------------------------------------------------------
# LMS JSON-RPC constants
# ---------------------------------------------------------------------------

LMS_HTTP_PORT = 9000             # Default LMS web interface port
LMS_JSONRPC_PATH = "/jsonrpc.js" # JSON-RPC endpoint path
LMS_QUERY_TIMEOUT_SEC = 3       # Timeout for JSON-RPC queries

# ---------------------------------------------------------------------------
# Status socket constants
# ---------------------------------------------------------------------------

STATUS_UPDATE_INTERVAL_SEC = 0.5 # How often to push status to connected clients
STATUS_SOCKET_PATH = "~/.squeezy/now_playing.sock"  # Unix socket for status

# ---------------------------------------------------------------------------
# Sync group constants
#
# When players are in a sync group, LMS coordinates them to start audio at
# the exact same moment. The jiffies-based start-at-time mechanism uses
# 32-bit unsigned arithmetic with wrap-around.
# ---------------------------------------------------------------------------

SYNC_START_WINDOW_MS = 10000     # Max ms into future we'll wait for sync target
JIFFIES_WRAP_GUARD = 0x7FFFFFFF  # Half of u32 range — values above this in a
                                 # diff indicate the target is in the past (wrapped)

# ---------------------------------------------------------------------------
# SETD (Set Data) protocol IDs
# ---------------------------------------------------------------------------

SETD_ID_PLAYER_NAME = 0         # ID 0 = player name query/set

# ---------------------------------------------------------------------------
# PCM format lookup tables
#
# These are used to decode the ASCII-digit-encoded PCM parameters in 'strm s'
# packets. The SlimProto protocol encodes sample size, rate, channels, and
# endianness as ASCII digit characters (cf. squeezelite pcm.c:65-67).
# ---------------------------------------------------------------------------

# Maps ASCII digit ordinal to bit depth (e.g., ord('1') → 16-bit)
PCM_SAMPLE_SIZE_MAP = {
    ord("0"): 8,
    ord("1"): 16,
    ord("2"): 20,
    ord("3"): 24,
    ord("4"): 32,
}

# Rate index table — the pcm_sample_rate byte is an ASCII digit index into
# this array (e.g., '3' → index 3 → 44100 Hz)
PCM_RATE_TABLE = [
    11025, 22050, 32000, 44100, 48000, 8000, 12000,
    16000, 24000, 96000, 88200, 176400, 192000, 352800, 384000,
]

# Signal strength reported in STAT packets (0xFFFF = wired connection)
SIGNAL_STRENGTH_WIRED = 0xFFFF

# Error codes for STAT packets
ERROR_NONE = 0

# ---------------------------------------------------------------------------
# SlimProto packet format strings
#
# All packets share the same wire envelope:
#
#   ┌─────────────┬──────────────────────────────────┐
#   │  opcode     │  payload_length  │  payload ...  │
#   │  4 bytes    │  4 bytes (u32be) │  N bytes      │
#   └─────────────┴──────────────────────────────────┘
#
# struct format notation:
#   ">"  = big-endian (network byte order)
#   "B"  = unsigned 8-bit int  (u8)
#   "H"  = unsigned 16-bit int (u16)
#   "I"  = unsigned 32-bit int (u32)
#   "s"  = raw bytes of fixed length (e.g. "4s" = 4-byte string)
# ---------------------------------------------------------------------------

# Shared envelope: opcode (4 bytes) + payload length (u32)
_PKT_HEADER_FMT = ">4sI"

# HELO payload: sent once at connect to introduce ourselves to LMS.
#   B   deviceid       — 12 = SqueezePlay (we impersonate this class)
#   B   revision       — always 0
#   6s  mac            — player MAC address (unique identity)
#   16s uuid           — 128-bit UUID (unused, all zeros)
#   H   wlan_channellist — 0x4000 on reconnect, 0x0000 on first connect
#   I   bytes_recv_H   — upper 32 bits of total bytes received (lifetime)
#   I   bytes_recv_L   — lower 32 bits of total bytes received
#   2s  lang           — ISO language code (unused, "\x00\x00")
#   ...capabilities string appended as raw ASCII bytes (no length prefix)
_HELO_PAYLOAD_FMT = ">BB6s16sHII2s"

# STAT payload: the heartbeat we send to LMS every few seconds and on events.
# LMS uses this to track playback position, buffer health, and sync timing.
#   4s  event_code     — 4-char ASCII event (e.g. "STMt"=timer, "STMs"=started)
#   B   num_crlf       — legacy field, always 0
#   B   mas_initialized — legacy MAS chip field, always 0
#   B   mas_mode       — legacy MAS chip field, always 0
#   I   stream_buf_size  — total size of our stream (download) buffer in bytes
#   I   stream_buf_full  — bytes currently in the stream buffer
#   I   bytes_recv_H   — upper 32 bits of bytes received from LMS stream server
#   I   bytes_recv_L   — lower 32 bits of bytes received
#   H   signal_strength — WiFi RSSI; 0xFFFF = wired (we always report wired)
#   I   jiffies        — our current timestamp in ms (see gettime_ms())
#   I   output_buf_size  — total size of our audio output buffer in bytes
#   I   output_buf_full  — bytes currently in the output buffer
#   I   elapsed_seconds  — seconds of audio played (u32 — NOT u16, easy mistake)
#   H   voltage        — battery voltage for portable devices; 0 for us
#   I   elapsed_ms     — milliseconds of audio played (more precise than above)
#   I   server_timestamp — echo of the jiffies value LMS sent in 'strm t'
#   H   error_code     — decoder error code; 0 = no error
_STAT_PAYLOAD_FMT = ">4sBBBIIIIHIIIIHIIH"

# NOTE on elapsed_seconds vs elapsed_ms field ordering:
# The field order is: ..., elapsed_seconds (I=u32), voltage (H=u16), elapsed_ms (I=u32)
# A common bug (we had it) is swapping the H and I: using u16 for elapsed_seconds
# makes it overflow at 65 seconds and shifts all subsequent fields by 2 bytes,
# causing LMS to read garbage for elapsed_ms and never advance the progress bar.


def gettime_ms():
    """Return a 32-bit millisecond timestamp — "jiffies" in the SlimProto protocol.

    LMS and all players share this same definition: milliseconds since the Unix
    epoch, masked to 32 bits.  The value wraps around every ~49.7 days, which is
    fine because the protocol only ever compares deltas (e.g. "start playing at
    jiffies X") and always guards against wrap-around with a window check.

    The & 0xFFFFFFFF keeps us in unsigned 32-bit range so our value matches what
    LMS sends in 'strm u' packets and what we echo back in every STAT packet.
    """
    return int(time.time() * 1000) & 0xFFFFFFFF


def mac_from_string(mac_str):
    """Convert MAC address string (e.g. "aa:bb:cc:dd:ee:ff") to bytes."""
    return bytes(int(b, 16) for b in mac_str.split(":"))


def default_mac():
    """Generate a MAC address from system UUID."""
    node = uuid.getnode()
    return node.to_bytes(6, "big")


def encode_replay_gain(gain_float):
    """Encode a floating-point gain value to 16.16 fixed-point format.

    Args:
        gain_float: Gain multiplier (1.0 = unity gain)

    Returns:
        32-bit unsigned integer in 16.16 fixed-point format
    """
    return int(round(gain_float * 0x10000)) & 0xFFFFFFFF


def decode_replay_gain(gain_raw):
    """Decode a 16.16 fixed-point replay gain value to float.

    The format is: upper 16 bits = integer part, lower 16 bits = fractional part.
    Example: 0x10000 = 1.0 (unity gain), 0x8000 = 0.5 (-6dB)

    Args:
        gain_raw: 32-bit unsigned integer in 16.16 fixed-point format

    Returns:
        Floating-point gain multiplier
    """
    return gain_raw / 0x10000


# ---------------------------------------------------------------------------
# SlimProto packet builders
#
# All of these are pure functions with no side effects, suitable for
# unit testing and reuse in other contexts.
# ---------------------------------------------------------------------------


def build_helo(mac, caps, reconnect=False, bytes_received=0):
    """Build a HELO (Hello) packet to introduce ourselves to LMS.

    Args:
        mac: 6-byte MAC address
        caps: Capabilities string (e.g. "Model=squeezelite...")
        reconnect: Bool — 0x4000 if True (reconnect), 0x0000 if False (first connect)
        bytes_received: Lifetime bytes received from LMS (u64)

    Returns:
        Complete HELO packet (header + payload)
    """
    caps_bytes = caps.encode("ascii")
    payload = struct.pack(
        _HELO_PAYLOAD_FMT,
        DEVICE_ID,                                    # deviceid (12 = SqueezePlay)
        0,                                            # revision
        mac,                                          # 6-byte MAC address
        b"\x00" * 16,                                 # uuid (unused)
        0x4000 if reconnect else 0x0000,              # wlan_channellist
        (bytes_received >> 32) & 0xFFFFFFFF,          # bytes_received_H
        bytes_received & 0xFFFFFFFF,                  # bytes_received_L
        b"\x00\x00",                                  # lang (unused)
    ) + caps_bytes                                    # capabilities string (variable length)
    header = struct.pack(_PKT_HEADER_FMT, b"HELO", len(payload))
    return header + payload


def build_stat(event, stream_buf_size=0, stream_buf_full=0,
               bytes_received=0, output_buf_size=0, output_buf_full=0,
               elapsed_ms=0, server_timestamp=0):
    """Build a STAT (Status) packet — the heartbeat we send to LMS.

    Args:
        event: 4-char event code (e.g. "STMt", "STMs", "STMd", "STMu")
        stream_buf_size: Total size of stream buffer (bytes)
        stream_buf_full: Bytes currently in stream buffer
        bytes_received: Lifetime bytes received from stream server (u64)
        output_buf_size: Total size of output (PCM) buffer (bytes)
        output_buf_full: Bytes currently in output buffer
        elapsed_ms: Milliseconds of audio played
        server_timestamp: Echo of the jiffies value from LMS 'strm t'

    Returns:
        Complete STAT packet (header + payload)
    """
    payload = struct.pack(
        _STAT_PAYLOAD_FMT,
        event.encode("ascii"),                        # event code e.g. b"STMt"
        0,                                            # num_crlf (legacy, unused)
        0,                                            # mas_initialized (legacy)
        0,                                            # mas_mode (legacy)
        stream_buf_size,                              # stream buffer total size
        stream_buf_full,                              # stream buffer bytes used
        (bytes_received >> 32) & 0xFFFFFFFF,          # bytes received (high u32)
        bytes_received & 0xFFFFFFFF,                  # bytes received (low u32)
        SIGNAL_STRENGTH_WIRED,                            # signal_strength: wired connection
        gettime_ms(),                                 # jiffies: our current ms clock
        output_buf_size,                              # output buffer total size
        output_buf_full,                              # output buffer bytes used
        elapsed_ms // 1000,                           # elapsed_seconds (u32)
        0,                                            # voltage (u16, 0 = not portable)
        elapsed_ms,                                   # elapsed_milliseconds (u32)
        server_timestamp,                             # echo of LMS's 'strm t' timestamp
        ERROR_NONE,                                   # error_code (0 = ok)
    )
    header = struct.pack(_PKT_HEADER_FMT, b"STAT", len(payload))
    return header + payload


def build_dsco(reason=0):
    """Build a DSCO (Disconnect) packet to tell LMS the stream disconnected.

    Args:
        reason: Reason code (0=ok, 1=local, 2=remote, 3=unreachable, 4=timeout)

    Returns:
        Complete DSCO packet
    """
    payload = struct.pack(">B", reason)
    header = struct.pack(_PKT_HEADER_FMT, b"DSCO", len(payload))
    return header + payload


def build_resp(http_headers):
    """Build a RESP (Response) packet to forward HTTP headers to LMS.

    Args:
        http_headers: Raw HTTP response headers (bytes)

    Returns:
        Complete RESP packet
    """
    header = struct.pack(_PKT_HEADER_FMT, b"RESP", len(http_headers))
    return header + http_headers


def build_setd(player_id, data):
    """Build a SETD (Set Data) packet to send a settings value to LMS.

    Args:
        player_id: Setting ID (0 = player name)
        data: Value as raw bytes (null-terminated for strings)

    Returns:
        Complete SETD packet
    """
    payload = struct.pack(">B", player_id) + data
    header = struct.pack(_PKT_HEADER_FMT, b"SETD", len(payload))
    return header + payload

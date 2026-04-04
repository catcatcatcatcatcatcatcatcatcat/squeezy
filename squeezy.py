#!/usr/bin/env python3
"""Squeezy - Minimal Squeezebox player for Lyrion Music Server."""

import argparse
import array
import errno
from importlib.metadata import version as pkg_version
import json
import logging
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from urllib.request import urlopen
import uuid

import miniaudio

log = logging.getLogger("squeezy")

SLIMPROTO_PORT = 3483
DEVICE_ID = 12  # squeezeplay device type
VERSION = pkg_version("squeezy")
STREAM_BUF_MAX = 2 * 1024 * 1024
SAMPLE_RATE = 44100
CHANNELS = 2
BYTES_PER_FRAME = 4  # 16-bit stereo = 4 bytes per frame
DEVICE_BUFFER_MSEC = 40   # miniaudio device buffer size (squeezelite uses 40ms for ALSA)

# Platform pipeline latency — the OS audio stack depth below miniaudio's buffer.
# squeezelite measures this directly via snd_pcm_delay() which returns the full
# hardware depth.  We can't query it through miniaudio, so we use a platform
# default for the layer between miniaudio and the DAC.
# Override at runtime with --latency.
import sys as _sys
if _sys.platform == "darwin":
    PLATFORM_PIPELINE_MSEC = 40   # CoreAudio HAL buffer + IOAudio kernel stack
elif _sys.platform == "win32":
    PLATFORM_PIPELINE_MSEC = 30   # WASAPI shared mode
else:
    PLATFORM_PIPELINE_MSEC = 10   # Linux ALSA (thinner stack)

DEVICE_DELAY_MSEC = DEVICE_BUFFER_MSEC + PLATFORM_PIPELINE_MSEC  # total fallback


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
    return bytes(int(b, 16) for b in mac_str.split(":"))


def default_mac():
    node = uuid.getnode()
    return node.to_bytes(6, "big")


# ---------------------------------------------------------------------------
# SlimProto packet builders
#
# All packets share the same wire envelope:
#
#   ┌─────────────┬──────────────────────────────────┐
#   │  opcode     │  payload_length  │  payload ...  │
#   │  4 bytes    │  4 bytes (u32be) │  N bytes      │
#   └─────────────┴──────────────────────────────────┘
#
# struct format notation used throughout:
#   ">"  = big-endian (network byte order) — all SlimProto fields are big-endian
#   "B"  = unsigned 8-bit int  (u8)
#   "H"  = unsigned 16-bit int (u16)
#   "I"  = unsigned 32-bit int (u32)
#   "s"  = raw bytes of fixed length (e.g. "4s" = 4-byte string)
#
# Reference: https://wiki.slimdevices.com/index.php/SlimProto_TCP_protocol
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


def build_helo(mac, caps, reconnect=False, bytes_received=0):
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
        0xFFFF,                                       # signal_strength: 0xFFFF = wired
        gettime_ms(),                                 # jiffies: our current ms clock
        output_buf_size,                              # output buffer total size
        output_buf_full,                              # output buffer bytes used
        elapsed_ms // 1000,                           # elapsed_seconds (u32)
        0,                                            # voltage (u16, 0 = not portable)
        elapsed_ms,                                   # elapsed_milliseconds (u32)
        server_timestamp,                             # echo of LMS's 'strm t' timestamp
        0,                                            # error_code (0 = ok)
    )
    header = struct.pack(_PKT_HEADER_FMT, b"STAT", len(payload))
    return header + payload


def build_dsco(reason=0):
    """DSCO — tell LMS the stream disconnected and why.

    reason codes: 0=ok, 1=local disconnect, 2=remote disconnect,
                  3=unreachable, 4=timeout
    """
    payload = struct.pack(">B", reason)               # single u8 reason code
    header = struct.pack(_PKT_HEADER_FMT, b"DSCO", len(payload))
    return header + payload


def build_resp(http_headers):
    """RESP — forward the HTTP response headers from the stream server to LMS.

    LMS uses these to confirm the stream connected and to read metadata like
    Content-Type and ICY headers.  We pass the raw header bytes unchanged.
    """
    header = struct.pack(_PKT_HEADER_FMT, b"RESP", len(http_headers))
    return header + http_headers


def build_setd(player_id, data):
    """SETD — send a settings value to LMS (e.g. confirm player name change).

    player_id 0 = player name.  data is the value as raw bytes (null-terminated
    for strings).
    """
    payload = struct.pack(">B", player_id) + data     # u8 id + variable data
    header = struct.pack(_PKT_HEADER_FMT, b"SETD", len(payload))
    return header + payload


# ---------------------------------------------------------------------------
# PCM buffer
#
# Three threads share this buffer:
#
#   Writer  (stream thread)  — downloads audio, optionally decodes via ffmpeg,
#                              calls .write() with raw s16le PCM bytes
#   Reader  (miniaudio cb)   — the audio generator runs in miniaudio's callback
#                              thread; calls .read() to pull frames to play
#   Control (main thread)    — calls .flush() on track stop/skip
#
# A plain threading.Lock() is sufficient because each operation is a single
# short critical section.  The buffer grows/shrinks dynamically; for a 3-min
# track at 44.1kHz/16-bit/stereo the peak size is ~30MB.
# ---------------------------------------------------------------------------

class PCMBuffer:
    def __init__(self):
        self.buf = bytearray()
        self.lock = threading.Lock()

    def write(self, data):
        with self.lock:
            self.buf.extend(data)

    def read(self, n):
        with self.lock:
            chunk = bytes(self.buf[:n])
            del self.buf[:n]
            return chunk

    def available(self):
        with self.lock:
            return len(self.buf)

    def skip(self, n):
        """Discard up to n bytes from the buffer. Return bytes actually skipped."""
        with self.lock:
            actual = min(n, len(self.buf))
            del self.buf[:actual]
            return actual

    def flush(self):
        with self.lock:
            self.buf.clear()


class StatusSocketServer:
    """Unix domain socket server for reporting playback status to clients (e.g., macOS menu bar widget)."""

    def __init__(self, squeezy_instance, socket_path):
        self.squeezy = squeezy_instance
        self.socket_path = socket_path
        self.running = True
        self.clients = []
        self._lock = threading.Lock()

    def run(self):
        """Listen for connections and broadcast status updates."""
        # Create socket directory if needed
        socket_dir = os.path.dirname(self.socket_path)
        if socket_dir and not os.path.exists(socket_dir):
            try:
                os.makedirs(socket_dir, mode=0o700)
            except OSError:
                pass

        # Remove old socket file if it exists
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass

        sock = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(self.socket_path)
            sock.listen(5)
            sock.settimeout(1)
            log.info("Status socket listening at %s", self.socket_path)

            while self.running:
                try:
                    client_sock, _ = sock.accept()
                    threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True).start()
                except socket.timeout:
                    continue
                except OSError:
                    break
        except Exception as e:
            log.warning("Status socket error: %s", e)
        finally:
            try:
                if sock:
                    sock.close()
            except Exception:
                pass
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

    def _handle_client(self, client_sock):
        """Send status updates to a connected client."""
        try:
            last_title = ""
            while self.running:
                try:
                    status = self.squeezy._status_dict()
                    status_json = json.dumps(status) + "\n"
                    client_sock.sendall(status_json.encode("utf-8"))

                    # Detect title changes for immediate updates
                    if status["title"] != last_title:
                        last_title = status["title"]

                    # Send every 500ms
                    time.sleep(0.5)
                except BrokenPipeError:
                    break
                except Exception:
                    break
        except Exception:
            pass
        finally:
            try:
                client_sock.close()
            except Exception:
                pass


# --- Squeezy Player ---

class Squeezy:
    def __init__(self, name="Squeezy", server=None, mac=None, device_id=None, latency_msec=None):
        self.name = name
        self.server_ip = server
        self.mac = mac_from_string(mac) if mac else default_mac()
        self.audio_device_id = device_id
        self.sock = None
        self.running = False
        self.reconnect = False
        self.bytes_received = 0
        self.server_timestamp = 0
        self._failed_connect_count = 0  # Reconnection fallback to UDP discovery

        # Stream state
        self.stream_sock = None
        self.stream_thread = None
        self.ffmpeg_proc = None
        self.decode_thread = None
        self.pcm_buf = PCMBuffer()
        self.streaming = False
        self.stream_bytes = 0
        self.decode_complete = False
        self.autostart = 0
        self.cont_received = False  # For autostart >= 2

        # Audio state
        self.device = None
        self.playing = False
        self.paused = False
        self.start_at_jiffies = 0
        self.output_frames = 0
        self.volume = 1.0  # 0.0–1.0, set by audg from LMS
        # OS pipeline latency below miniaudio (overridable via --latency)
        self.pipeline_latency_msec = latency_msec if latency_msec is not None else PLATFORM_PIPELINE_MSEC

        # Sample rate tracking (variable sample rate support, like squeezelite)
        self.current_sample_rate = 44100   # Active playback rate
        self.next_sample_rate = 44100      # Upcoming track rate
        self.supported_rates = [44100, 48000, 96000, 192000]

        # Dynamic device delay tracking — like squeezelite's snd_pcm_delay().
        # We derive buffer occupancy from wall clock: frames_yielded - frames_played.
        # Set when the first real audio frame (non-silence) is sent to the device.
        self._device_start_time = None   # monotonic time of first real audio frame
        self._device_start_frames = 0    # output_frames value at that moment

        # STAT flags (match squeezelite: only send each once per track)
        self.sent_STMd = False
        self.sent_STMu = False
        self.sent_STMo = False
        self.sent_STMl = False

        # Track queue — like squeezelite, when a new strm-s arrives while
        # we're still playing, we queue it and let the current track drain
        # rather than killing the output buffer.
        self._pending_track = None
        self._track_done = threading.Event()

        # Status reporting — track metadata and playback info for socket
        # ICY metadata (from in-stream headers, for radio/Shoutcast)
        self.icy_title = ""
        self.icy_artist = ""
        self.icy_album = ""
        self.icy_meta_int = 0  # ICY metadata interval in bytes (0 = no metadata)

        # LMS metadata (from JSON-RPC API query)
        # "requesting..." = query in progress, "" = not queried, actual value = result from LMS
        self.lms_title = ""
        self.lms_artist = ""
        self.lms_album_artist = ""
        self.lms_album = ""
        self.lms_year = ""
        self.lms_duration_ms = 0

        # Status socket
        self.current_stream_url = ""  # Stream URL
        self._status_socket_started = False
        self._status_server = None
        self._status_thread = None

        # Server timeout detection (35-second heartbeat)
        self._last_server_msg = time.monotonic()

        self._send_lock = threading.Lock()

    def _get_supported_rate(self, requested_rate):
        """Return the closest supported sample rate, or 44100 as fallback.

        Supported rates: [44100, 48000, 96000, 192000]
        For unsupported rates (8kHz, 16kHz, etc.), fall back to 44100.
        """
        if requested_rate in self.supported_rates:
            return requested_rate
        # Fall back to 44100 for unsupported rates
        return 44100

    def _detect_ffmpeg_rate(self, ffmpeg_proc, timeout=2.0):
        """Detect sample rate from ffmpeg stderr output.

        Looks for pattern: "Stream #0:0: Audio: <codec>, <rate> Hz"
        Returns the detected rate, or None if not detected within timeout.
        """
        try:
            import select
            # Try to read from ffmpeg stderr with timeout
            start = time.time()
            while time.time() - start < timeout:
                # Use select (Unix) or try a short blocking read
                ready = select.select([ffmpeg_proc.stderr], [], [], 0.1)
                if ready[0]:
                    line = ffmpeg_proc.stderr.readline().decode('utf-8', errors='ignore')
                    if line:
                        # Look for "Stream #0:0: Audio: mp3, 48000 Hz"
                        match = re.search(r'(\d+)\s+Hz', line)
                        if match:
                            rate = int(match.group(1))
                            log.debug("Detected ffmpeg sample rate: %d Hz", rate)
                            return rate
        except (ImportError, AttributeError):
            # select not available (Windows) or ffmpeg_proc.stderr issues
            pass
        except Exception as e:
            log.debug("Error detecting ffmpeg rate: %s", e)
        return None

    def _capabilities(self):
        # The capabilities string is sent in the HELO packet and tells LMS
        # what this player can do.  LMS uses it to decide:
        #   - Which codec to use (we list what ffmpeg can decode for us)
        #   - Whether to send AccuratePlayPoints (we say yes — we track frames)
        #   - What model name to show in the LMS UI
        #   - The maximum sample rate we support (currently hardcoded to 44100)
        #
        # Key fields (from squeezelite/slimproto.c BASE_CAP):
        #   Model=squeezelite  — tells LMS to treat us like a squeezelite player
        #   ModelName=…        — display name in the LMS interface
        #   AccuratePlayPoints=1 — we report frame-accurate elapsed time
        #   HasDigitalOut=1    — enables some LMS UI features
        #   Firmware=VERSION   — shown in LMS player settings
        #   MaxSampleRate=N    — highest rate we'll accept (LMS won't send higher)
        #   pcm,mp3,flac,…     — codec list; LMS picks the first one it can serve
        return (
            f"Model=squeezelite,ModelName={self.name},"
            f"AccuratePlayPoints=1,HasDigitalOut=1,HasPolarityInversion=1,"
            f"Firmware={VERSION},MaxSampleRate={SAMPLE_RATE},"
            f"pcm,mp3,flac,ogg,aac"
        )

    # --- Network ---

    def discover(self):
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
                    sock.sendto(b"e", (bcast, SLIMPROTO_PORT))
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

    def _send(self, data):
        with self._send_lock:
            try:
                self.sock.sendall(data)
            except OSError as e:
                log.warning("Send error: %s", e)

    def _send_stat(self, event, server_timestamp=0):
        elapsed = self._elapsed_ms()
        # Suppress repetitive STMt logging when idle
        if event != "STMt" or self.playing:
            title = self.lms_title or self.icy_title or "Unknown"
            state = "playing" if self.playing else "paused" if self.paused else "idle"
            log.debug("STAT %s [%s] \"%s\" - %s (frames=%d bytes=%d)",
                      event, self._format_elapsed(elapsed), title, state,
                      self.output_frames, self.stream_bytes)
        pkt = build_stat(
            event,
            stream_buf_size=STREAM_BUF_MAX,
            stream_buf_full=self.pcm_buf.available(),
            bytes_received=self.stream_bytes,
            output_buf_size=SAMPLE_RATE * BYTES_PER_FRAME * 10,
            output_buf_full=self.pcm_buf.available(),
            elapsed_ms=elapsed,
            server_timestamp=server_timestamp,
        )
        self._send(pkt)

    def _format_elapsed(self, elapsed_ms):
        """Convert milliseconds to MM:SS format for readable logging."""
        total_sec = elapsed_ms // 1000
        minutes = total_sec // 60
        seconds = total_sec % 60
        return f"{minutes}:{seconds:02d}"

    def _elapsed_ms(self):
        """Frame-based elapsed time with dynamic device delay compensation.

        Like squeezelite's snd_pcm_delay() approach (slimproto.c:163-166):
        subtract the actual buffer occupancy so LMS knows what the user
        *hears*, not what we've fed to the audio device.

        We derive buffer depth from wall clock time:
            buffer = frames_yielded - (wall_time_elapsed * sample_rate)

        This is equivalent to querying the hardware buffer directly —
        if we've yielded 500ms of audio and 460ms of wall time has passed,
        there's 40ms still sitting in the device buffer.

        Falls back to the static DEVICE_DELAY_MSEC constant until we have
        enough data to measure dynamically (first real audio frame).
        """
        if self.output_frames == 0:
            return 0

        if self._device_start_time is None:
            # Not yet playing real audio — use static estimate
            device_delay_frames = SAMPLE_RATE * (DEVICE_BUFFER_MSEC + self.pipeline_latency_msec) // 1000
        else:
            # Dynamic miniaudio buffer depth: frames yielded minus frames played
            # (equivalent to snd_pcm_delay() for the miniaudio layer)
            frames_since = self.output_frames - self._device_start_frames
            ms_since = (time.monotonic() - self._device_start_time) * 1000
            buffer_ms = frames_since * 1000 / SAMPLE_RATE - ms_since
            # Clamp to sane range
            buffer_ms = max(0.0, min(buffer_ms, DEVICE_BUFFER_MSEC * 2))
            # Add the OS pipeline below miniaudio (CoreAudio/ALSA/WASAPI layer)
            # that snd_pcm_delay() would include but we can't query directly
            total_delay_ms = buffer_ms + self.pipeline_latency_msec
            device_delay_frames = int(total_delay_ms * SAMPLE_RATE / 1000)

        frames = max(0, self.output_frames - device_delay_frames)
        return int(frames * 1000 / SAMPLE_RATE)

    def connect(self):
        if not self.server_ip:
            self.server_ip = self.discover()
            if not self.server_ip:
                log.error("No server found")
                return False

        log.info("Connecting to %s:%d", self.server_ip, SLIMPROTO_PORT)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        try:
            self.sock.connect((self.server_ip, SLIMPROTO_PORT))
        except OSError as e:
            log.error("Connection failed: %s", e)
            return False

        self.sock.settimeout(1)

        helo = build_helo(self.mac, self._capabilities(), reconnect=self.reconnect,
                          bytes_received=self.stream_bytes)
        self._send(helo)
        self.reconnect = True
        log.info("Connected, HELO sent (MAC: %s)", ":".join(f"{b:02x}" for b in self.mac))
        return True

    # --- Message loop ---

    def run(self):
        self.running = True
        while self.running:
            if not self.connect():
                self._failed_connect_count += 1
                # After 5 consecutive failures, fall back to UDP discovery
                if self._failed_connect_count >= 5:
                    log.info("Failed to connect to %s 5 times — falling back to UDP discovery",
                             self.server_ip or "server")
                    self.server_ip = None
                    self._failed_connect_count = 0
                log.info("Retrying in 5 seconds...")
                time.sleep(5)
                continue
            # Success — reset failure counter
            self._failed_connect_count = 0
            try:
                self._message_loop()
            except Exception as e:
                log.warning("Connection lost: %s", e)
            finally:
                self._stop_playback()
                try:
                    self.sock.close()
                except OSError:
                    pass
            if self.running:
                log.info("Reconnecting in 2 seconds...")
                time.sleep(2)

    def _message_loop(self):
        # LMS sends messages in a simple framing protocol:
        #
        #   ┌──────────────────┬──────────────────────────┐
        #   │  length (u16be)  │  payload (length bytes)  │
        #   └──────────────────┴──────────────────────────┘
        #
        # The payload begins with a 4-byte ASCII opcode ("strm", "audg", etc.)
        # followed by opcode-specific fields.  Multiple messages can arrive in
        # one TCP recv(), so we accumulate into `buf` and parse in a loop.
        #
        # Timeout behaviour: the socket has a 1-second recv() timeout.
        # If no data arrives for 35 consecutive seconds we treat the connection
        # as dead and reconnect.  LMS normally sends a 'strm t' every ~5 seconds
        # while a player is active; 35s matches squeezelite's timeout threshold
        # and accommodates mysqueezebox.com which can go silent for up to 30s.
        buf = bytearray()
        expect_len = None
        timeouts = 0
        last_status = 0

        while self.running:
            # Periodic STMt heartbeat — LMS uses these to track elapsed time
            # and drive the progress bar.  squeezelite sends every ~1 second.
            now = time.time()
            if self.playing and not self.paused and now - last_status > 1.0:
                self._send_stat("STMt")
                last_status = now

            try:
                data = self.sock.recv(4096)
                if not data:
                    log.info("Server closed connection")
                    return
                timeouts = 0
                self._last_server_msg = time.monotonic()  # Reset timeout on any data received
                buf.extend(data)
            except socket.timeout:
                timeouts += 1
                # Check elapsed time since last message — if > 35 seconds, connection is dead
                elapsed = time.monotonic() - self._last_server_msg
                if elapsed > 35:
                    log.info("No messages from server for %.0fs — connection dead, reconnecting", elapsed)
                    return
                continue

            # Parse all complete messages out of the accumulation buffer
            while True:
                if expect_len is None:
                    if len(buf) < 2:
                        break                         # need more data for length prefix
                    expect_len = struct.unpack(">H", buf[:2])[0]
                    buf = buf[2:]

                if len(buf) < expect_len:
                    break                             # message body not fully arrived yet

                msg = bytes(buf[:expect_len])
                buf = buf[expect_len:]
                expect_len = None
                self._handle_message(msg)

    def _handle_message(self, msg):
        if len(msg) < 4:
            return
        opcode = msg[:4]
        log.debug("Received: %s (%d bytes)", opcode, len(msg))

        handlers = {
            b"strm": self._handle_strm,
            b"audg": self._handle_audg,
            b"setd": self._handle_setd,
            b"aude": self._handle_aude,
            b"cont": self._handle_cont,
            b"serv": self._handle_serv,
        }
        handler = handlers.get(opcode)
        if handler:
            handler(msg)
        else:
            log.debug("Unhandled opcode: %s", opcode)

    # --- Message handlers ---

    def _handle_strm(self, msg):
        if len(msg) < 5:
            return
        command = chr(msg[4])
        log.debug("strm command: %s", command)

        if command == "t":
            # Timing request - echo server timestamp
            if len(msg) >= 22:
                ts = struct.unpack_from(">I", msg, 18)[0]
                self._send_stat("STMt", server_timestamp=ts)
            else:
                self._send_stat("STMt")

        elif command == "s":
            self._handle_strm_start(msg)

        elif command == "p":
            # Pause — replay_gain field = interval in ms (0 = immediate)
            interval = 0
            if len(msg) >= 22:
                interval = struct.unpack_from(">I", msg, 18)[0]
            if interval:
                log.debug("Pause with interval %d ms (treating as immediate)", interval)
            if self.playing and not self.paused:
                self.paused = True
                if self.device:
                    try:
                        self.device.close()
                    except Exception:
                        pass
                    self.device = None
            # Always confirm pause to LMS (squeezelite sends STMp regardless of interval)
            self._send_stat("STMp")

        elif command == "u":
            # Unpause with optional sync timestamp (used for multi-room sync).
            # Like squeezelite: if jiffies is non-zero, enter start-at-time
            # mode (play silence until target jiffies reached, then start).
            # If zero, start immediately.
            target_jiffies = 0
            if len(msg) >= 22:
                target_jiffies = struct.unpack_from(">I", msg, 18)[0]

            self.start_at_jiffies = target_jiffies
            log.debug("unpause at: %d now: %d", target_jiffies, gettime_ms())
            if self.paused:
                self.paused = False
                self._resume_audio()
            elif not self.playing and self.pcm_buf.available() > 0:
                # Not yet playing (e.g., sync mode: we sent STMl but LMS
                # hadn't told us to start yet). Start audio now — the
                # generator will output silence until start_at_jiffies.
                if target_jiffies:
                    self._start_audio_at_time()
                else:
                    self._start_audio()
            self._send_stat("STMr")

        elif command == "a":
            # Skip ahead — replay_gain field = milliseconds to skip
            if len(msg) >= 22:
                skip_ms = struct.unpack_from(">I", msg, 18)[0]
                skip_frames = int(skip_ms * SAMPLE_RATE / 1000)
                skip_bytes = skip_frames * BYTES_PER_FRAME
                actual = self.pcm_buf.skip(skip_bytes)
                skipped_frames = actual // BYTES_PER_FRAME
                self.output_frames += skipped_frames
                log.debug("Skip ahead: %d ms (%d frames requested, %d skipped)",
                         skip_ms, skip_frames, skipped_frames)
            self._send_stat("STMc")

        elif command == "q":
            # Quit streaming entirely — hard stop, always report completion
            # This command tells the player to stop immediately and disconnect.
            # We always send STMf to confirm we've stopped.
            log.debug("Quit command: stopping playback and stream")
            self._stop_playback()
            self._send_stat("STMf")

        elif command == "f":
            # Flush output buffer — graceful stop that may allow track queuing
            # This tells the player to flush the current output buffer and prepare
            # for the next track. We only send STMf if we were actually playing.
            # This allows for gapless transitions when a new strm-s arrives.
            was_active = self.streaming or self.playing
            log.debug("Flush command: stopping current playback (was_active=%s)", was_active)
            self._stop_playback()
            if was_active:
                self._send_stat("STMf")

    def _handle_strm_start(self, msg):
        # 'strm s' packet layout (all offsets are from start of payload):
        #
        #  off  len  field
        #   0    4   opcode "strm"
        #   4    1   command 's'
        #   5    1   autostart  ASCII digit: '0'=immediate, '1'=output-buffer,
        #                        '2'=wait-for-CONT, '3'=wait-for-CONT+output-buffer
        #                        (sync mode uses 2 or 3; CONT decrements it by 2)
        #   6    1   format     codec: 'm'=mp3, 'f'=flac, 'p'=pcm, 'o'=ogg,
        #                        'a'=aac, 'w'=wma, 'l'=alac (Apple), 'e'=aac-he
        #   7    1   pcm_sample_size  ASCII: '0'=8, '1'=16, '2'=20, '3'=24, '4'=32
        #   8    1   pcm_sample_rate  ASCII digit index into rate table (see below)
        #   9    1   pcm_channels     ASCII: '1'=mono, '2'=stereo
        #  10    1   pcm_endianness   ASCII: '0'=big-endian, '1'=little-endian
        #  11    1   threshold  output buffer threshold in 1KB units (threshold*1024)
        #  12    1   spdif_enable
        #  13    1   transition_period  crossfade seconds
        #  14    1   transition_type    0=none,1=crossfade,2=fade-in,3=fade-out,4=in+out
        #  15    1   flags
        #  16    1   output_threshold
        #  17    1   slaves (sync)
        #  18    4   replay_gain  (u32, fixed-point 16.16 — 0x10000 = unity)
        #  22    2   server_port  (u16be)
        #  24    4   server_ip    (u32be, 0 = use slimproto server address)
        #  28    …   http_header  raw HTTP request bytes to send to stream server
        if len(msg) < 28:
            log.warning("strm 's' packet too short")
            return

        self.autostart = msg[5] - ord("0") if msg[5] >= ord("0") else 0
        fmt = chr(msg[6])
        pcm_sample_size = msg[7]
        pcm_sample_rate = msg[8]
        pcm_channels = msg[9]
        pcm_endian = msg[10]
        threshold = msg[11] * 1024
        server_port = struct.unpack_from(">H", msg, 22)[0]
        server_ip_raw = struct.unpack_from(">I", msg, 24)[0]
        http_header = msg[28:]

        # server_ip == 0 means "same host as the LMS slimproto connection"
        if server_ip_raw == 0:
            server_ip = self.server_ip
        else:
            server_ip = socket.inet_ntoa(struct.pack(">I", server_ip_raw))

        # PCM format fields use ASCII digit encoding from squeezelite's pcm.c.
        # They're only meaningful when fmt == 'p' (raw PCM); for compressed
        # formats (mp3, flac, etc.) ffmpeg auto-detects from the stream.
        pcm_info = None
        if fmt == "p":
            size_map = {ord("0"): 8, ord("1"): 16, ord("2"): 20, ord("3"): 24, ord("4"): 32}
            bits = size_map.get(pcm_sample_size, 16)
            # Rate is an index into this fixed table (squeezelite/pcm.c:65-67)
            rate_table = [11025, 22050, 32000, 44100, 48000, 8000, 12000,
                          16000, 24000, 96000, 88200, 176400, 192000, 352800, 384000]
            rate_idx = pcm_sample_rate - ord("0") if pcm_sample_rate >= ord("0") else 0
            rate = rate_table[rate_idx] if rate_idx < len(rate_table) else 44100
            chans = pcm_channels - ord("0") if pcm_channels >= ord("0") else 2
            if chans not in (1, 2):
                chans = 2
            endian = "le" if pcm_endian == ord("1") else "be"
            pcm_info = {"bits": bits, "rate": rate, "channels": chans, "endian": endian}

        # Detect sample rate for this stream (variable sample rate support)
        if fmt == "p" and pcm_info:
            self.next_sample_rate = self._get_supported_rate(pcm_info["rate"])
        else:
            # For compressed formats, will detect from ffmpeg output later
            self.next_sample_rate = 44100  # Default, may be updated by ffmpeg detection

        log.debug("Stream start: format=%s server=%s:%d threshold=%d autostart=%d pcm=%s",
                  fmt, server_ip, server_port, threshold, self.autostart, pcm_info)

        stream_args = (server_ip, server_port, http_header, threshold, self.autostart, fmt, pcm_info)

        # Like squeezelite: if audio is still playing from the previous track
        # (decode done, buffer draining), don't kill the output — queue the
        # next track and let the current one finish.
        if self.playing and self.decode_complete:
            log.debug("Track still playing — queuing next track for gapless transition")
            self._pending_track = stream_args
            self._send_stat("STMf")
            # Stop the old *stream* (network) but keep the audio device running
            self.streaming = False
            if self.stream_sock:
                try:
                    self.stream_sock.close()
                except Exception:
                    pass
            return

        # Stop any existing playback
        self._stop_playback()
        self._send_stat("STMf")
        self._start_stream(stream_args)

    def _start_stream(self, stream_args):
        """Begin streaming a track (called for fresh start or after drain)."""
        server_ip, server_port, http_header, threshold, autostart, fmt, pcm_info = stream_args
        self.streaming = True
        self.stream_bytes = 0
        self.decode_complete = False
        self.cont_received = (autostart < 2)  # autostart < 2 doesn't need cont
        self.pcm_buf.flush()
        self.autostart = autostart
        # Reset STAT flags for new track (like squeezelite)
        self.sent_STMd = False
        self.sent_STMu = False
        self.sent_STMo = False
        self.sent_STMl = False

        # Reset progress tracking for new track
        self.output_frames = 0
        self._device_start_time = None
        self._device_start_frames = 0

        # Reset metadata for new track
        self.icy_title = ""
        self.icy_artist = ""
        self.icy_album = ""
        self.lms_title = ""
        self.lms_artist = ""
        self.lms_album_artist = ""
        self.lms_album = ""
        self.lms_year = ""
        self.lms_duration_ms = 0

        # Extract and store stream URL from HTTP header
        try:
            http_str = http_header.decode("utf-8", errors="ignore") if isinstance(http_header, bytes) else http_header
            # Extract URL from "GET <url> HTTP/1.1"
            parts = http_str.split("\r\n")[0].split(" ")
            if len(parts) >= 2:
                self.current_stream_url = parts[1]
        except Exception:
            self.current_stream_url = ""

        # Spawn status socket server on first stream start
        if not self._status_socket_started:
            socket_path = os.path.expanduser("~/.squeezy/now_playing.sock")
            try:
                self._status_server = StatusSocketServer(self, socket_path)
                self._status_thread = threading.Thread(target=self._status_server.run, daemon=True)
                self._status_thread.start()
                self._status_socket_started = True
                log.debug("Status socket started at %s", socket_path)
            except Exception as e:
                log.warning("Failed to start status socket: %s", e)

        # Query LMS for track metadata in a separate thread
        query_thread = threading.Thread(target=self._query_lms_track_info, daemon=True)
        query_thread.start()

        self.stream_thread = threading.Thread(
            target=self._stream_worker,
            args=(server_ip, server_port, http_header, threshold, autostart, fmt, pcm_info),
            daemon=True,
        )
        self.stream_thread.start()

    def _start_pending_track(self):
        """Start the queued next track after current track finishes draining."""
        args = self._pending_track
        self._pending_track = None
        if args:
            log.debug("Starting queued next track")
            self._stop_playback()
            self._start_stream(args)

    def _handle_audg(self, msg):
        if len(msg) >= 22:
            adjust = msg[12]
            gain_l = struct.unpack_from(">I", msg, 14)[0]
            gain_r = struct.unpack_from(">I", msg, 18)[0]
            # Use left channel gain as volume (mono mix); max gain is 0x10000 = 1.0
            self.volume = min(gain_l / 0x10000, 1.0) if adjust else 1.0
            log.debug("Volume: %.0f%% (L=%.2f R=%.2f adjust=%d)",
                      self.volume * 100, gain_l / 0x10000, gain_r / 0x10000, adjust)

    def _handle_setd(self, msg):
        if len(msg) < 5:
            return
        setd_id = msg[4]
        if setd_id == 0:
            if len(msg) == 5:
                # Query player name
                name_data = self.name.encode("utf-8") + b"\x00"
                self._send(build_setd(0, name_data))
            elif len(msg) > 5:
                # Set player name
                new_name = msg[5:].rstrip(b"\x00").decode("utf-8", errors="replace")
                if new_name:
                    self.name = new_name
                    log.info("Player name set to: %s", self.name)
                name_data = self.name.encode("utf-8") + b"\x00"
                self._send(build_setd(0, name_data))

    def _handle_aude(self, msg):
        log.debug("aude received")

    def _handle_cont(self, msg):
        log.debug("cont received (autostart was %d)", self.autostart)
        if self.autostart >= 2:
            self.autostart -= 2
            self.cont_received = True

    def _handle_serv(self, msg):
        if len(msg) >= 8:
            new_ip = struct.unpack_from(">I", msg, 4)[0]
            if new_ip:
                self.server_ip = socket.inet_ntoa(struct.pack(">I", new_ip))
                log.info("Server redirect to %s", self.server_ip)

    # --- Status Reporting ---

    def _query_lms_track_info(self):
        """Query LMS for current track metadata via JSON-RPC API.

        Uses our MAC address as player_id to identify ourselves to LMS.
        Runs in a separate thread to avoid blocking stream startup.
        """
        if not self.server_ip:
            return

        try:
            # Set title to "requesting..." while query is in progress
            self.lms_title = "requesting..."

            # Convert MAC address to string format: "aa:bb:cc:dd:ee:ff"
            player_id = ":".join(f"{b:02x}" for b in self.mac)

            # Query all metadata fields in a single request
            # Note: LMS may not have year/albumartist for all tracks; genre is available
            results = self._lms_query_batch(player_id, [
                "title", "artist", "album", "duration", "genre"
            ])

            # Extract results
            self.lms_title = results.get("title") or ""
            self.lms_artist = results.get("artist") or ""
            # Album artist falls back to regular artist if not available
            self.lms_album_artist = self.lms_artist
            self.lms_album = results.get("album") or ""
            # Year not reliably available, so we'll leave it empty
            self.lms_year = ""

            duration_sec = results.get("duration")
            if duration_sec:
                try:
                    self.lms_duration_ms = int(float(duration_sec) * 1000)
                except (ValueError, TypeError):
                    self.lms_duration_ms = 0

            # Log all track info when we have the title
            if self.lms_title and self.lms_title != "requesting...":
                duration_str = ""
                if self.lms_duration_ms > 0:
                    mins = self.lms_duration_ms // 60000
                    secs = (self.lms_duration_ms % 60000) // 1000
                    duration_str = f" [{mins}:{secs:02d}]"

                # Format: [album] artist - title
                # Artist falls back to albumartist
                artist = self.lms_artist or self.lms_album_artist or "Unknown"
                album = self.lms_album or "Unknown Album"
                log.info("🎵 [%s] %s - %s%s",
                         album,
                         artist,
                         self.lms_title,
                         duration_str)

        except Exception as e:
            log.debug("LMS query failed: %s", e)
            # Clear "requesting..." state on error
            self.lms_title = ""

    def _lms_query(self, player_id, command, is_numeric=False):
        """Query LMS for a single field via JSON-RPC.

        Args:
            player_id: MAC address string ("aa:bb:cc:dd:ee:ff")
            command: LMS command ("title", "artist", "album", "duration", etc.)
            is_numeric: If True, parse result as number

        Returns:
            String or numeric value, or None if query fails
        """
        try:
            from urllib.request import Request, urlopen

            payload = json.dumps({
                "id": 1,
                "method": "slim.request",
                "params": [player_id, [command, "?"]],
            }).encode()

            url = f"http://{self.server_ip}:9000/jsonrpc.js"
            req = Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
            )

            with urlopen(req, timeout=3) as resp:
                result_dict = json.loads(resp.read())
                result = result_dict.get("result", {})
                value = result.get(f"_{command}")

                if is_numeric and value is not None:
                    return float(value)
                return value

        except Exception as e:
            log.debug("LMS query for '%s' failed: %s", command, e)
            return None

    def _lms_query_batch(self, player_id, fields):
        """Query LMS for multiple fields efficiently.

        Makes sequential requests for all fields in a single method call,
        avoiding thread spawn overhead while handling each field's potential failure.

        Args:
            player_id: MAC address string ("aa:bb:cc:dd:ee:ff")
            fields: List of field names to query (["title", "artist", "album", etc.])

        Returns:
            Dictionary of field_name: value pairs. Missing fields are not included.
        """
        result_dict = {}
        for field in fields:
            value = self._lms_query(player_id, field)
            if value is not None:
                result_dict[field] = value
        return result_dict

    def _parse_icy_metadata(self, data):
        """Parse ICY metadata block and extract title, artist, album.

        ICY metadata format: 1 byte length (in 16-byte units), then length*16 bytes
        of key=value pairs separated by semicolons.
        Example: b"StreamTitle='Song Name';StreamUrl='...';StreamArtist='Artist';"

        Returns True if title changed.
        """
        if len(data) < 1:
            return False

        meta_len = data[0]
        if meta_len == 0:
            return False

        meta_bytes = meta_len * 16
        if len(data) < 1 + meta_bytes:
            return False

        try:
            meta_str = data[1:1+meta_bytes].decode("utf-8", errors="ignore").rstrip("\x00")
            title_changed = False

            # Parse key=value pairs
            for pair in meta_str.split(";"):
                pair = pair.strip()
                if "=" not in pair:
                    continue
                key, val = pair.split("=", 1)
                key = key.strip().lower()
                # Remove quotes if present
                val = val.strip().strip("'\"")

                if key == "streamtitle":
                    if val != self.icy_title:
                        self.icy_title = val
                        log.info("Track: %s (from: ICY metadata)", val)
                        title_changed = True
                elif key == "streamartist":
                    self.icy_artist = val
                elif key == "streamalbum":
                    self.icy_album = val

            return title_changed
        except Exception as e:
            log.debug("ICY metadata parse error: %s", e)
            return False

    def _status_dict(self):
        """Return current playback status as a dictionary."""
        return {
            "title": self.lms_title or self.icy_title or "Unknown",
            "artist": self.lms_artist or self.icy_artist or "",
            "album_artist": self.lms_album_artist or "",
            "album": self.lms_album or "",
            "year": self.lms_year or "",
            "elapsed_ms": self._elapsed_ms(),
            "total_ms": self.lms_duration_ms,
            "playing": self.playing,
            "paused": self.paused,
        }

    # --- Streaming ---

    def _stream_worker(self, server_ip, server_port, http_header, threshold, autostart, fmt="?", pcm_info=None):
        try:
            self._do_stream(server_ip, server_port, http_header, threshold, autostart, fmt, pcm_info)
        except Exception as e:
            log.warning("Stream error: %s", e)
        finally:
            # Wait for decode reader to finish reading ffmpeg output
            # (don't kill ffmpeg — let it close naturally after stdin is closed)
            if self.decode_thread and self.decode_thread.is_alive():
                self.decode_thread.join(timeout=10)
            self.streaming = False
            self._cleanup_ffmpeg()
            try:
                self._send(build_dsco(0))
            except Exception:
                pass

    def _do_stream(self, server_ip, server_port, http_header, threshold, autostart, fmt="?", pcm_info=None):
        # Connect to stream server
        log.debug("Connecting to stream %s:%d", server_ip, server_port)
        self.stream_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.stream_sock.settimeout(10)
        self.stream_sock.connect((server_ip, server_port))

        # Send HTTP request
        self.stream_sock.sendall(http_header)

        # Read HTTP response headers
        resp_buf = bytearray()
        while b"\r\n\r\n" not in resp_buf:
            chunk = self.stream_sock.recv(4096)
            if not chunk:
                log.warning("Stream closed during headers")
                return
            resp_buf.extend(chunk)

        header_end = resp_buf.index(b"\r\n\r\n") + 4
        resp_headers = bytes(resp_buf[:header_end])
        leftover = bytes(resp_buf[header_end:])

        log.debug("Stream response headers:\n%s", resp_headers.decode("ascii", errors="replace"))

        # Parse ICY metadata interval (for in-stream metadata like Shoutcast)
        self.icy_meta_int = 0
        try:
            headers_str = resp_headers.decode("ascii", errors="replace")
            for line in headers_str.split("\r\n"):
                if line.lower().startswith("icy-metaint:"):
                    self.icy_meta_int = int(line.split(":", 1)[1].strip())
                    log.debug("ICY metadata interval: %d bytes", self.icy_meta_int)
                    break
        except Exception:
            pass

        # Send RESP and STMc
        self._send(build_resp(resp_headers))
        self._send_stat("STMc")

        # For raw PCM at our native format, skip ffmpeg entirely
        pcm_passthrough = (fmt == "p" and pcm_info
                           and pcm_info["bits"] == 16 and pcm_info["endian"] == "le"
                           and pcm_info["rate"] == SAMPLE_RATE
                           and pcm_info["channels"] == CHANNELS)

        if pcm_passthrough:
            log.debug("PCM passthrough (no ffmpeg needed)")
            # Feed HTTP body directly to PCM buffer
            self._stream_to_buffer(leftover, threshold, autostart)
        else:
            # Build ffmpeg command — specify input format for raw PCM
            ffmpeg_cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
            if fmt == "p" and pcm_info:
                ffmpeg_cmd += ["-f", "s{0}{1}".format(pcm_info["bits"], pcm_info["endian"]),
                               "-ar", str(pcm_info["rate"]),
                               "-ac", str(pcm_info["channels"])]
            ffmpeg_cmd += ["-i", "pipe:0",
                           "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
                           "pipe:1"]
            log.debug("ffmpeg command: %s", " ".join(ffmpeg_cmd))

            self.ffmpeg_proc = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # For compressed formats, detect sample rate from ffmpeg output
            if fmt != "p":
                detected_rate = self._detect_ffmpeg_rate(self.ffmpeg_proc)
                if detected_rate:
                    self.next_sample_rate = self._get_supported_rate(detected_rate)
                    log.debug("Using detected ffmpeg rate: %d Hz", self.next_sample_rate)

            # Start decode reader thread
            self.decode_thread = threading.Thread(
                target=self._decode_reader,
                args=(threshold, autostart),
                daemon=True,
            )
            self.decode_thread.start()

            # Feed data to ffmpeg
            self._stream_to_ffmpeg(leftover)

    def _check_threshold_start(self, threshold, autostart_hint, force=False):
        """Check if buffer threshold is reached and handle start/readiness.

        The autostart field in 'strm s' controls the sync handshake:

          autostart=1  Normal playback — start as soon as buffer threshold is
                       reached.  Send STMs when audio begins.

          autostart=2  Sync group — LMS wants all players to start together.
                       Flow: strm-s(autostart=2) → CONT → STMl → strm-u(jiffies)
                       1. We receive 'strm s' with autostart=2
                       2. We wait for CONT (LMS sends this when ready for us to buffer)
                       3. CONT handler decrements autostart: 2 → 0
                       4. We buffer audio until threshold, then send STMl ("I'm ready")
                       5. LMS collects STMl from ALL synced players
                       6. LMS sends 'strm u' with a shared jiffies target to everyone
                       7. All players start simultaneously at that target time

        Uses self.autostart (live value, updated by CONT handler) rather than
        the hint passed in at stream-start time, since CONT changes it.
        """
        if self.playing or self.sent_STMl:
            return  # Already started or signalled

        avail = self.pcm_buf.available()
        if not force and avail < max(threshold, 8192):
            return  # Threshold not yet reached

        # Use the live autostart value (CONT may have decremented it)
        autostart = self.autostart

        if autostart >= 1:
            # Normal mode: start audio immediately
            self._start_audio()
            self._send_stat("STMs")
        elif autostart == 0 and not self.sent_STMl:
            # Sync mode: signal readiness to LMS, don't start audio yet.
            # LMS will send 'strm u' with jiffies once all synced players
            # have reported STMl.
            self.sent_STMl = True
            log.info("Buffer threshold reached — signalling ready (STMl) for sync")
            self._send_stat("STMl")

    def _stream_to_buffer(self, leftover, threshold, autostart):
        """Stream raw PCM directly to the PCM buffer (no ffmpeg)."""
        started = False
        log.debug("PCM passthrough: leftover=%d bytes, streaming=%s", len(leftover), self.streaming)

        if leftover:
            self.stream_bytes += len(leftover)
            self.pcm_buf.write(leftover)

        self.stream_sock.settimeout(5)
        while self.streaming and self.running:
            try:
                data = self.stream_sock.recv(32768)
                if not data:
                    break
                self.stream_bytes += len(data)
                self.pcm_buf.write(data)

                if not started and self.cont_received:
                    self._check_threshold_start(threshold, autostart)
                    started = self.playing or self.sent_STMl

            except socket.timeout:
                continue
            except OSError as e:
                # Retry on transient errors, break on permanent errors
                if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.ECONNRESET, errno.ECONNABORTED):
                    log.debug("Stream recv transient error %d — retrying", e.errno)
                    time.sleep(0.1)
                    continue
                # Permanent error or unknown
                log.debug("Stream error: %s", e)
                break

        # If we never started but have data, try now
        if not started and self.pcm_buf.available() > 0 and self.cont_received:
            self._check_threshold_start(threshold, autostart, force=True)

        self.decode_complete = True
        avail = self.pcm_buf.available()
        log.debug("PCM stream complete, %d bytes buffered (started=%s)", avail, started)

        try:
            self.stream_sock.close()
        except Exception:
            pass
        self.stream_sock = None

    def _stream_to_ffmpeg(self, leftover):
        """Feed HTTP stream data to ffmpeg stdin, parsing ICY metadata if present."""
        buf = bytearray(leftover) if leftover else bytearray()
        bytes_since_meta = 0

        if leftover:
            self.stream_bytes += len(leftover)

        self.stream_sock.settimeout(5)
        while self.streaming and self.running:
            try:
                chunk = self.stream_sock.recv(32768)
                if not chunk:
                    break
                self.stream_bytes += len(chunk)
                buf.extend(chunk)

                # Parse ICY metadata if enabled
                if self.icy_meta_int > 0:
                    while len(buf) > 0:
                        # Check if we've accumulated enough audio data for metadata
                        if bytes_since_meta < self.icy_meta_int:
                            need = self.icy_meta_int - bytes_since_meta
                            if len(buf) >= need:
                                # Write audio data up to metadata point
                                try:
                                    self.ffmpeg_proc.stdin.write(bytes(buf[:need]))
                                except BrokenPipeError:
                                    return
                                buf = buf[need:]
                                bytes_since_meta = self.icy_meta_int
                            else:
                                # Not enough data yet, write what we have and continue
                                try:
                                    self.ffmpeg_proc.stdin.write(bytes(buf))
                                except BrokenPipeError:
                                    return
                                bytes_since_meta += len(buf)
                                buf.clear()
                                break

                        # Now try to parse metadata
                        if bytes_since_meta >= self.icy_meta_int:
                            if len(buf) < 1:
                                break  # Need at least 1 byte for metadata length
                            meta_len = buf[0]
                            meta_bytes = meta_len * 16
                            if len(buf) < 1 + meta_bytes:
                                break  # Need full metadata block
                            # Extract and parse metadata
                            meta_block = bytes(buf[:1+meta_bytes])
                            self._parse_icy_metadata(meta_block)
                            buf = buf[1+meta_bytes:]
                            bytes_since_meta = 0
                else:
                    # No ICY metadata, just write data to ffmpeg
                    try:
                        self.ffmpeg_proc.stdin.write(bytes(buf))
                    except BrokenPipeError:
                        return
                    buf.clear()

            except socket.timeout:
                continue
            except OSError as e:
                # Retry on transient errors, break on permanent errors
                if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.ECONNRESET, errno.ECONNABORTED):
                    log.debug("Stream recv transient error %d — retrying", e.errno)
                    time.sleep(0.1)
                    continue
                # Permanent error or unknown
                log.debug("Stream error: %s", e)
                break

        # Flush any remaining data in buffer
        if buf and self.ffmpeg_proc and self.ffmpeg_proc.stdin:
            try:
                self.ffmpeg_proc.stdin.write(bytes(buf))
            except BrokenPipeError:
                pass

        # Close ffmpeg stdin to signal EOF
        try:
            self.ffmpeg_proc.stdin.close()
        except Exception:
            pass

        # Close stream socket
        try:
            self.stream_sock.close()
        except Exception:
            pass
        self.stream_sock = None

    def _decode_reader(self, threshold, autostart):
        """Read decoded PCM from ffmpeg stdout into PCM buffer."""
        started = False
        log.debug("Decode reader started (threshold=%d autostart=%d)", threshold, autostart)
        while self.running:
            try:
                if not self.ffmpeg_proc:
                    log.debug("Decode reader: ffmpeg_proc is None, exiting")
                    break
                data = self.ffmpeg_proc.stdout.read(8192)
                if not data:
                    log.debug("Decode reader: ffmpeg stdout closed")
                    break
                self.pcm_buf.write(data)

                # Check threshold for auto-start or sync readiness
                if not started and self.cont_received:
                    self._check_threshold_start(threshold, autostart)
                    started = self.playing or self.sent_STMl

            except Exception as e:
                log.debug("Decode reader exception: %s", e)
                break

        # If we never started but have data, try now
        if not started and self.pcm_buf.available() > 0 and self.cont_received:
            self._check_threshold_start(threshold, autostart, force=True)

        # Check ffmpeg exit code — send error packet if non-zero
        if self.ffmpeg_proc:
            exit_code = self.ffmpeg_proc.returncode
            if exit_code and exit_code != 0:
                log.warning("ffmpeg exited with code %d — sending error packet", exit_code)
                self._send_stat("STMn")  # STMn = error packet
            else:
                # Mark decode complete — STMd+STMu are sent from the audio
                # generator when the buffer is fully drained (track finished)
                self.decode_complete = True
        else:
            self.decode_complete = True

        log.debug("Decode complete, %d bytes buffered", self.pcm_buf.available())

    def _cleanup_ffmpeg(self):
        if self.ffmpeg_proc:
            try:
                self.ffmpeg_proc.stdin.close()
            except Exception:
                pass
            try:
                self.ffmpeg_proc.kill()
                self.ffmpeg_proc.wait(timeout=2)
            except Exception:
                pass
            self.ffmpeg_proc = None

    # --- Audio output ---

    def _audio_generator(self):
        """Generator that yields PCM data for miniaudio playback.
        miniaudio sends framecount via send(), we yield bytes back."""
        required_frames = yield b""  # priming yield
        while self.playing and self.running:
            if self.paused:
                required_frames = yield b"\x00" * (required_frames * BYTES_PER_FRAME)
                continue

            required_bytes = required_frames * BYTES_PER_FRAME

            # Sync: if start_at_jiffies is set, output silence until target time
            # (like squeezelite's OUTPUT_START_AT state)
            if self.start_at_jiffies:
                now = gettime_ms()
                diff = (self.start_at_jiffies - now) & 0xFFFFFFFF
                if diff < 0x7FFFFFFF and diff > 0 and diff < 10000:
                    required_frames = yield b"\x00" * required_bytes
                    continue
                # Target reached or passed — clear and start real audio
                log.debug("Sync target reached (target=%d now=%d) — starting audio",
                          self.start_at_jiffies, now)
                self.start_at_jiffies = 0
                self.output_frames = 0  # Reset for accurate elapsed time
                self._send_stat("STMs")  # Tell LMS track started

            avail = self.pcm_buf.available()
            if avail > 0:
                n = min(avail, required_bytes)
                chunk = self.pcm_buf.read(n)
                if chunk:
                    # Mark the wall-clock moment the first real audio frame
                    # is handed to the device — used for dynamic delay tracking.
                    if self._device_start_time is None:
                        self._device_start_time = time.monotonic()
                        self._device_start_frames = self.output_frames
                    self.output_frames += len(chunk) // BYTES_PER_FRAME
                    # Send STMd once when decode completes (like squeezelite).
                    # The new strm-s from LMS is handled by queuing, not by
                    # killing current output.
                    if self.decode_complete and not self.sent_STMd:
                        self.sent_STMd = True
                        self._send_stat("STMd")
                    if len(chunk) < required_bytes:
                        chunk += b"\x00" * (required_bytes - len(chunk))
                    # Apply volume scaling (s16le samples)
                    vol = self.volume
                    if vol < 0.999:
                        samples = array.array("h", chunk)
                        for i in range(len(samples)):
                            samples[i] = int(samples[i] * vol)
                        chunk = samples.tobytes()
                    required_frames = yield chunk
                    continue

            # Buffer empty
            if self.decode_complete and avail == 0:
                # Track fully played — send STMu (output underrun)
                if not self.sent_STMu:
                    self.sent_STMu = True
                    self._send_stat("STMu")
                break
            elif self.streaming and avail == 0 and not self.sent_STMo:
                # Buffer underrun while still streaming — send STMo
                self.sent_STMo = True
                self._send_stat("STMo")

            # Yield silence while waiting for data
            required_frames = yield b"\x00" * required_bytes

        # Generator exiting — track finished or stopped
        self.playing = False
        log.info("Playback finished")

        # If a next track was queued (LMS sent strm-s while we were
        # draining), start it now — like squeezelite's track boundary.
        if self._pending_track:
            threading.Thread(target=self._start_pending_track, daemon=True).start()

    def _start_audio(self):
        if self.playing:
            return
        log.info("Starting audio playback")
        try:
            self.device = miniaudio.PlaybackDevice(
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=CHANNELS,
                sample_rate=SAMPLE_RATE,
                buffersize_msec=DEVICE_BUFFER_MSEC,
                device_id=self.audio_device_id,
            )
            log.debug("Audio device buffer: %dms (requested %dms)",
                      self.device.buffersize_msec, DEVICE_BUFFER_MSEC)
            self.playing = True  # Set before start() — generator checks this immediately
            self.paused = False
            self.output_frames = 0
            self._device_start_time = None   # Reset dynamic delay tracking
            self._device_start_frames = 0
            gen = self._audio_generator()
            next(gen)  # prime the generator before miniaudio calls send()
            self.device.start(gen)
        except Exception as e:
            log.error("Audio start failed: %s", e)

    def _start_audio_at_time(self):
        """Start audio device immediately but output silence until sync timestamp.
        The generator handles the silence-until-time logic (OUTPUT_START_AT equivalent)."""
        log.debug("Sync start at jiffies=%d (now=%d)", self.start_at_jiffies, gettime_ms())
        self._start_audio()

    def _resume_audio(self):
        if not self.playing:
            self._start_audio()
            return
        log.info("Resuming audio (%d bytes buffered)", self.pcm_buf.available())
        # Close old device before creating new one
        if self.device:
            try:
                self.device.close()
            except Exception:
                pass
            self.device = None
        try:
            self.device = miniaudio.PlaybackDevice(
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=CHANNELS,
                sample_rate=SAMPLE_RATE,
                buffersize_msec=DEVICE_BUFFER_MSEC,
                device_id=self.audio_device_id,
            )
            gen = self._audio_generator()
            next(gen)  # prime the generator
            self.device.start(gen)
        except Exception as e:
            log.error("Audio resume failed: %s", e)

    def _stop_playback(self):
        self.streaming = False
        self.playing = False
        self.paused = False
        self.output_frames = 0
        self.decode_complete = False
        self._pending_track = None       # Clear any queued track
        self._device_start_time = None   # Reset dynamic delay tracking
        self._device_start_frames = 0

        # Close stream socket to unblock any pending recv
        if self.stream_sock:
            try:
                self.stream_sock.close()
            except Exception:
                pass
            self.stream_sock = None

        # Wait for stream thread to finish so it doesn't interfere with a new stream
        if self.stream_thread and self.stream_thread.is_alive():
            self.stream_thread.join(timeout=5)
        self.stream_thread = None
        self.decode_thread = None
        self.cont_received = False
        self.sent_STMd = False
        self.sent_STMu = False
        self.sent_STMo = False
        self.sent_STMl = False

        if self.device:
            try:
                self.device.close()
            except Exception:
                pass
            self.device = None

        self._cleanup_ffmpeg()
        self.pcm_buf.flush()

    def stop(self):
        log.info("Shutting down...")
        self.running = False
        self._stop_playback()


def list_audio_devices():
    """List available audio output devices."""
    devs = miniaudio.Devices()
    playbacks = devs.get_playbacks()
    # The first device miniaudio returns is the system default
    print("Audio output devices:")
    for i, d in enumerate(playbacks):
        tag = " (default)" if i == 0 else ""
        fmts = ", ".join(f"{f['samplerate']}Hz" for f in d.get("formats", [])[:3])
        print(f"  {i}: {d['name']}{tag}  [{fmts}]")
    return playbacks


def find_device_id(name):
    """Find a playback device by name (case-insensitive substring match)."""
    devs = miniaudio.Devices()
    playbacks = devs.get_playbacks()
    needle = name.lower()
    for d in playbacks:
        if needle in d["name"].lower():
            return d["id"], d["name"]
    return None, None


def _detect_install_method():
    """Detect how squeezy was installed to suggest the right upgrade command."""
    exe = sys.executable
    if "/linuxbrew/" in exe or "/homebrew/" in exe or "/Cellar/" in exe:
        return "brew upgrade squeezy"
    if "/.local/pipx/" in exe or "/pipx/" in exe:
        return "pipx upgrade squeezy"
    return "pip install --upgrade squeezy"


def check_for_update():
    """Check PyPI for a newer version. Runs in a background thread, never blocks."""
    try:
        resp = urlopen("https://pypi.org/pypi/squeezy/json", timeout=3)
        data = json.loads(resp.read())
        latest = data["info"]["version"]
        if latest != VERSION:
            cmd = _detect_install_method()
            log.warning("squeezy %s is available (you have %s). "
                        "Upgrade: %s", latest, VERSION, cmd)
    except Exception:
        pass  # Network down, PyPI unreachable — silently skip


def main():
    parser = argparse.ArgumentParser(description="Squeezy - Minimal Squeezebox player")
    parser.add_argument("-s", "--server", help="LMS server IP (auto-discover if not set)")
    parser.add_argument("-n", "--name", default="Squeezy", help="Player name (default: Squeezy)")
    parser.add_argument("-m", "--mac", help="MAC address aa:bb:cc:dd:ee:ff (auto-detect if not set)")
    parser.add_argument("-d", "--device", help="Audio output device (name or substring, e.g. 'HDMI')")
    parser.add_argument("-l", "--list-devices", action="store_true", help="List audio output devices and exit")
    parser.add_argument("--latency", type=int, default=None, metavar="MS",
                        help=f"OS audio pipeline latency in ms (default: {PLATFORM_PIPELINE_MSEC}ms on this platform). "
                             "Increase if sync is behind, decrease if ahead.")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="Increase verbosity (-v info, -vv debug)")
    parser.add_argument("--version", action="version", version=f"squeezy {VERSION}")
    args = parser.parse_args()

    if args.verbose >= 2:
        level = logging.DEBUG
    elif args.verbose == 1:
        level = logging.INFO
    else:
        level = logging.WARNING

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Non-blocking update check
    threading.Thread(target=check_for_update, daemon=True).start()

    if args.list_devices:
        list_audio_devices()
        return

    # Resolve audio device
    device_id = None
    if args.device:
        device_id, device_name = find_device_id(args.device)
        if device_id is None:
            log.error("No audio device matching '%s'. Use -l to list devices.", args.device)
            return
        log.info("Audio output: %s", device_name)
    else:
        log.info("Audio output: system default")

    player = Squeezy(name=args.name, server=args.server, mac=args.mac,
                     device_id=device_id, latency_msec=args.latency)

    def handle_signal(sig, frame):
        player.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    player.run()


if __name__ == "__main__":
    main()

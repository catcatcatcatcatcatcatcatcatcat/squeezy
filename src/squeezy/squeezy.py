#!/usr/bin/env python3
"""Squeezy — a Python player for Lyrion Music Server (LMS).

This is the main orchestrator module. It implements the SlimProto protocol
client that registers with LMS, receives playback commands, fetches audio
streams, and plays them through the system audio device.

Architecture overview:

    LMS ←→ SlimProto TCP ←→ Squeezy._message_loop()
                                  │
                                  ├── _handle_strm_start() → _start_stream()
                                  │       └── _stream_worker() [stream thread]
                                  │              ├── _do_stream() — HTTP fetch
                                  │              ├── _stream_to_ffmpeg() or _stream_to_buffer()
                                  │              └── _decode_reader() [decode thread]
                                  │
                                  └── _audio_generator() [miniaudio callback thread]
                                          └── reads PCMBuffer → DAC

Threading model:
    main thread          — SlimProto TCP message loop (recv/send)
    stream thread        — HTTP download → ffmpeg stdin (or direct PCM)
    decode thread        — ffmpeg stdout → PCMBuffer
    miniaudio cb thread  — audio generator reads PCMBuffer → DAC

See CLAUDE.md for protocol details, known bugs, and architecture decisions.
"""

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
import ssl
import struct
import subprocess
import sys
import threading
import time
from urllib.request import urlopen

import miniaudio

# Foundation modules
from .protocol import slimproto
from .config import config
from .config import metadata

# Network and protocol modules
from .network import server_connection
from .network import lms_metadata
from .network import status_server

# Audio buffer (PCMBuffer is the only class used from the audio package)
from .audio.stream_decoder import PCMBuffer

# Message dispatch — all protocol parsing lives in handler.py
from .protocol import handler as protocol_handler

log = logging.getLogger("squeezy")

VERSION = pkg_version("squeezy")


# --- Squeezy Player ---

class Squeezy:
    """SlimProto player implementation for Lyrion Music Server.

    Manages the full lifecycle of a player: discovery, registration, message
    handling, audio streaming, and playback. All state lives on this class;
    helper modules (AudioPlayer, StreamDecoder, ProtocolHandler) hold a
    reference back to this instance for shared state access.
    """

    @staticmethod
    def _load_player_name():
        """Load saved player name from XDG config directory."""
        return config.load_player_name()

    @staticmethod
    def _save_player_name(name):
        """Persist player name to XDG config directory."""
        config.save_player_name(name)

    def __init__(self, name=None, server=None, mac=None, device_id=None,
                 latency_msec=None, buffer_size_kb=None):
        """Initialize the Squeezy player.

        Args:
            name: Player name shown in LMS UI (default: saved name or "Squeezy").
                  CLI -n flag overrides the saved name.
            server: LMS server IP. If None, uses UDP broadcast discovery.
            mac: MAC address string ("aa:bb:cc:dd:ee:ff"). If None, auto-detected.
            device_id: miniaudio device ID for audio output. If None, uses default.
            latency_msec: Override for OS audio pipeline latency (for sync tuning).
            buffer_size_kb: PCM buffer size in KB (default: 4096, range: 64-8192).
        """
        # CLI name always wins; otherwise use saved name; otherwise default
        if name:
            self.name = name
        else:
            saved_name = self._load_player_name()
            self.name = saved_name if saved_name else "Squeezy"
        self.server_ip = server
        self.mac = slimproto.mac_from_string(mac) if mac else slimproto.default_mac()
        self.audio_device_id = device_id
        self.sock = None
        self.running = False
        self.reconnect = False
        self.bytes_received = 0
        self.server_timestamp = 0
        self._failed_connect_count = 0  # Reconnection fallback to UDP discovery

        # Audio buffer (thread-safe, shared by stream/decode/miniaudio threads)
        # --buffer-size CLI flag overrides default 4MB; clamped to 64KB-8MB range
        if buffer_size_kb is not None:
            clamped_kb = max(64, min(buffer_size_kb, 8192))
            if clamped_kb != buffer_size_kb:
                log.warning("Buffer size clamped to %dKB (requested %dKB)", clamped_kb, buffer_size_kb)
            self.pcm_buf = PCMBuffer(max_size=clamped_kb * 1024)
            log.info("PCM buffer: %dKB", clamped_kb)
        else:
            self.pcm_buf = PCMBuffer()

        # Message dispatch — all protocol parsing lives in handler.py
        self.protocol = protocol_handler.ProtocolHandler(self)

        # Stream state
        # Thread safety note: stream_sock, ffmpeg_proc are written by main thread
        # (in _stop_playback) and read/written by stream thread. Protected by the
        # fact that _stop_playback joins the stream thread before modifying them.
        self.stream_sock = None
        self.stream_thread = None
        self.ffmpeg_proc = None
        self.decode_thread = None
        # pcm_buf already initialized above via stream_decoder.PCMBuffer()
        # (has its own internal lock for thread-safe read/write/flush)
        self.streaming = False          # Written by main+stream threads; bool assignment is atomic in CPython
        self.stream_bytes = 0
        self.decode_complete = False    # Written by stream/decode thread, read by audio generator
        self.autostart = 0
        self.cont_received = False  # For autostart >= 2

        # Audio state
        # Thread safety: playing/paused are written by main thread (handlers),
        # read by audio generator (miniaudio callback thread). Bool assignment
        # is atomic in CPython (GIL). The critical invariant is that
        # self.playing = True MUST be set BEFORE device.start(gen) because
        # on Linux the callback fires immediately.
        self.device = None
        self.playing = False
        self.paused = False
        self.start_at_jiffies = 0
        self.output_frames = 0
        self.volume = 1.0  # 0.0–1.0, set by audg from LMS
        self.replay_gain = 1.0  # 1.0 = unity, set from strm 's' packet (16.16 fixed-point)
        # OS pipeline latency below miniaudio (overridable via --latency)
        self.pipeline_latency_msec = latency_msec if latency_msec is not None else slimproto.PLATFORM_PIPELINE_MSEC

        # Sample rate tracking (variable sample rate support, like squeezelite)
        self.current_sample_rate = 44100   # Active playback rate
        self.next_sample_rate = 44100      # Upcoming track rate
        self.supported_rates = slimproto.SUPPORTED_SAMPLE_RATES

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

        # True gapless playback — track boundaries
        self._current_track_id = 0  # Incremented for each new track
        self._track_start_frames = 0  # output_frames value when current track started
        self._switching_track = False  # Flag: generator should switch to pending track

        # MP3 gapless metadata (LAME encoder delay/padding, parsed by stream_decoder)
        self.lame_gapless = None  # dict with enc_delay, enc_padding, total_samples or None

        # Status reporting — track metadata and playback info for socket
        # ICY metadata (from in-stream headers, for radio/Shoutcast)
        self.icy_title = ""
        self.icy_artist = ""
        self.icy_album = ""
        self.icy_meta_int = 0  # ICY metadata interval in bytes (0 = no metadata)

        # Crossfade / fade parameters (per-track, extracted from strm 's' packet)
        self._crossfade_enabled = False    # Is crossfade currently active?
        self._fade_in_gains = None         # Gain curve for fade-in (built on transition)
        self._fade_out_gains = None        # Gain curve for fade-out (built on transition)
        self._crossfade_samples = []       # Ring buffer of old track samples at boundary
        self._crossfade_pos = 0            # Current position in crossfade window
        self._crossfade_total = 0          # Total fade samples for this crossfade window
        self.transition_type = 0           # Fade mode: 0=none, 1=crossfade, 2=fade-in, 3=fade-out, 4=in+out
        self.transition_period_sec = 0     # Fade duration in seconds (from strm 's' packet)

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

    @staticmethod
    def _probe_ffmpeg_codecs():
        """Probe ffmpeg for available decoders.

        Returns a list of codec short names that ffmpeg supports.
        Falls back to a standard list if probing fails.
        """
        standard_codecs = ["pcm", "mp3", "flac", "ogg", "aac"]

        try:
            result = subprocess.run(
                ["ffmpeg", "-decoders"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode != 0:
                return standard_codecs

            # Parse decoder list: look for common audio codecs
            # Format is roughly: " DEA... libopus ..."
            decoders = set()
            for line in result.stdout.split('\n'):
                if not line.startswith(' '):
                    continue
                # Extract codec name (after DEA flags)
                parts = line.split()
                if len(parts) < 2:
                    continue
                codec = parts[1]

                # Map ffmpeg codec names to SlimProto short names
                codec_map = {
                    'mp3': 'mp3', 'libmp3lame': 'mp3',
                    'flac': 'flac',
                    'pcm_s16le': 'pcm', 'pcm_s24le': 'pcm',
                    'vorbis': 'ogg', 'libvorbis': 'ogg',
                    'aac': 'aac', 'aac_fixed': 'aac',
                    'alac': 'alc',
                    'libopus': 'ops', 'opus': 'ops',
                    'wmav1': 'wma', 'wmav2': 'wma', 'wmalossless': 'wma',
                    'dsf': 'dsd', 'dsd_lsbf': 'dsd',
                }

                if codec in codec_map:
                    decoders.add(codec_map[codec])

            # Always include PCM as fallback
            decoders.add('pcm')

            result_list = sorted(list(decoders))
            if result_list:
                log.debug("Detected ffmpeg decoders: %s", result_list)
                return result_list
        except Exception as e:
            log.debug("Failed to probe ffmpeg codecs: %s, using defaults", e)

        return standard_codecs

    def _capabilities(self):
        """Build the HELO capabilities string for LMS registration.

        Returns a comma-separated string describing this player's features,
        model, firmware version, supported codecs, and maximum sample rate.
        LMS uses this to decide which codec to stream, what UI to show, and
        whether to send frame-accurate timing requests.

        Probes ffmpeg at startup to discover available decoders dynamically.
        """
        # The capabilities string is sent in the HELO packet and tells LMS
        # what this player can do.  LMS uses it to decide:
        #   - Which codec to use (we list what ffmpeg can decode for us)
        #   - Whether to send AccuratePlayPoints (we say yes — we track frames)
        #   - What model name to show in the LMS UI
        #   - The maximum sample rate we support (44.1k, 48k, 96k, 192k)
        #
        # Key fields (from squeezelite/slimproto.c BASE_CAP):
        #   Model=squeezelite  — tells LMS to treat us like a squeezelite player
        #   ModelName=…        — display name in the LMS interface
        #   AccuratePlayPoints=1 — we report frame-accurate elapsed time
        #   HasDigitalOut=1    — enables some LMS UI features
        #   Firmware=VERSION   — shown in LMS player settings
        #   MaxSampleRate=N    — highest rate we'll accept (LMS won't send higher)
        #   pcm,mp3,flac,…     — codec list; LMS picks the first one it can serve

        # Probe ffmpeg for available decoders (P2.11: Codec Priority)
        codecs = self._probe_ffmpeg_codecs()
        codec_str = ",".join(codecs)

        return (
            f"Model=squeezelite,ModelName={self.name},"
            f"AccuratePlayPoints=1,HasDigitalOut=1,HasPolarityInversion=1,"
            f"CanHTTPS=1,"  # Advertise HTTPS/SSL support
            f"Firmware={VERSION},MaxSampleRate=192000,"
            f"{codec_str}"
        )

    # --- Network ---

    def discover(self):
        """Discover LMS on the local network via UDP broadcast.

        Sends a discovery probe on the SlimProto port and listens for LMS
        responses. Tries multiple broadcast addresses because 255.255.255.255
        fails on some macOS network configurations.

        Returns:
            Server IP address string, or None if no server found.
        """
        return server_connection.ServerConnection.discover_lms(slimproto.SLIMPROTO_PORT)

    def _send(self, data):
        """Send a raw packet to LMS over the SlimProto TCP connection (thread-safe)."""
        with self._send_lock:
            try:
                self.sock.sendall(data)
            except OSError as e:
                log.warning("Send error: %s", e)

    def _send_stat(self, event, server_timestamp=0):
        """Send a STAT heartbeat packet to LMS with current playback state.

        STAT packets are the primary way we communicate state to LMS. They carry:
        - Event code (STMt=timer, STMs=started, STMd=decode done, STMu=underrun, etc.)
        - Buffer occupancy (how full our stream and output buffers are)
        - Elapsed time (how far into the current track we are)
        - Server timestamp echo (for round-trip timing / sync)
        """
        elapsed = self._elapsed_ms()
        # Suppress repetitive STMt logging when idle
        if event != "STMt" or self.playing:
            title = self.lms_title or self.icy_title or "Unknown"
            state = "playing" if self.playing else "paused" if self.paused else "idle"
            log.debug("STAT %s [%s] \"%s\" - %s (frames=%d bytes=%d)",
                      event, self._format_elapsed(elapsed), title, state,
                      self.output_frames, self.stream_bytes)
        pkt = slimproto.build_stat(
            event,
            stream_buf_size=slimproto.STREAM_BUF_MAX,
            stream_buf_full=self.pcm_buf.available(),
            bytes_received=self.stream_bytes,
            output_buf_size=self.current_sample_rate * slimproto.BYTES_PER_FRAME * 10,
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
        """Frame-based elapsed time with static device delay compensation.

        Like squeezelite's approach (slimproto.c:163-166): subtract the
        device buffer depth from frames_played so LMS knows what the user
        *hears*, not what we've fed to the audio device.

        We use a fixed delay estimate (DEVICE_BUFFER_MSEC + pipeline_latency_msec)
        rather than a dynamic wall-clock measurement, because the dynamic
        approach introduces jitter of 10-50ms per heartbeat. That jitter
        triggers LMS's sync adjustment logic, causing audible skips in
        synchronized playback. A fixed offset is more stable — squeezelite
        gets stable values from snd_pcm_delay(), but miniaudio doesn't
        expose hardware buffer depth, so a fixed estimate is our best option.

        For gapless: elapsed time is relative to current track boundary
        (output_frames since current track started).
        """
        # For true gapless, calculate relative to current track
        frames_in_track = self.output_frames - self._track_start_frames
        if frames_in_track <= 0:
            return 0

        # Fixed device delay: miniaudio buffer + OS audio pipeline
        device_delay_frames = self.current_sample_rate * (slimproto.DEVICE_BUFFER_MSEC + self.pipeline_latency_msec) // 1000

        frames = max(0, frames_in_track - device_delay_frames)
        return int(frames * 1000 / self.current_sample_rate)

    def connect(self):
        """Connect to LMS via TCP and send the HELO registration packet.

        If no server IP is configured, runs UDP discovery first. On success,
        the socket is ready for the message loop.

        Returns:
            True if connected and HELO sent, False on failure.
        """
        if not self.server_ip:
            print("Discovering server...", flush=True)
            self.server_ip = self.discover()
            if not self.server_ip:
                print("No server found.", flush=True)
                log.error("No server found")
                return False

        log.info("Connecting to %s:%d", self.server_ip, slimproto.SLIMPROTO_PORT)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(slimproto.CONNECT_TIMEOUT_SEC)
        try:
            self.sock.connect((self.server_ip, slimproto.SLIMPROTO_PORT))
        except OSError as e:
            log.error("Connection failed: %s", e)
            return False

        self.sock.settimeout(slimproto.RECV_TIMEOUT_SEC)

        helo = slimproto.build_helo(self.mac, self._capabilities(), reconnect=self.reconnect,
                          bytes_received=self.stream_bytes)
        self._send(helo)
        first_connect = not self.reconnect
        self.reconnect = True
        log.info("Connected, HELO sent (MAC: %s)", ":".join(f"{b:02x}" for b in self.mac))
        if first_connect:
            print(f"Connected to {self.server_ip}. Ready.", flush=True)
        return True

    # --- Message loop ---

    def _run_cleanup(self):
        """Run _stop_playback in a background thread so the main thread
        stays in Python code and can handle signals (Ctrl+C).

        C extension calls like device.close() block signal delivery
        because Python only runs signal handlers between bytecodes.
        """
        cleanup = threading.Thread(target=self._stop_playback, daemon=True)
        cleanup.start()
        while cleanup.is_alive():
            cleanup.join(timeout=0.2)

    def run(self):
        """Main player loop — connect, handle messages, reconnect on failure.

        Runs until self.running is set to False (by stop() or signal handler).
        On connection failure, retries with exponential back-off to UDP discovery
        after FAILED_CONNECT_THRESHOLD consecutive failures.
        """
        self.running = True
        try:
            while self.running:
                if not self.connect():
                    self._failed_connect_count += 1
                    if self._failed_connect_count >= slimproto.FAILED_CONNECT_THRESHOLD:
                        log.info("Failed to connect to %s %d times — falling back to UDP discovery",
                                 self.server_ip or "server", slimproto.FAILED_CONNECT_THRESHOLD)
                        self.server_ip = None
                        self._failed_connect_count = 0
                    log.info("Retrying in %d seconds...", slimproto.RETRY_DELAY_SEC)
                    # Sleep in small increments so we can respond to shutdown signals
                    for _ in range(int(slimproto.RETRY_DELAY_SEC * 10)):
                        if not self.running:
                            break
                        time.sleep(0.1)
                    continue
                self._failed_connect_count = 0
                try:
                    self._message_loop()
                except Exception as e:
                    if self.running:
                        log.warning("Connection lost: %s", e)
                finally:
                    self._run_cleanup()
                    if self.sock:
                        try:
                            self.sock.close()
                        except OSError:
                            pass
                        self.sock = None
                if self.running:
                    log.info("Reconnecting in %d seconds...", slimproto.RECONNECT_DELAY_SEC)
                    for _ in range(int(slimproto.RECONNECT_DELAY_SEC * 10)):
                        if not self.running:
                            break
                        time.sleep(0.1)
        finally:
            # Final cleanup — ensure everything is stopped
            self._run_cleanup()
            if self.sock:
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None
            log.info("Shutdown complete")

    def _message_loop(self):
        """Read and dispatch SlimProto messages from LMS until disconnect.

        This is the main event loop. It runs on the main thread and handles
        the SlimProto TCP framing protocol, periodic heartbeats, and server
        timeout detection.
        """
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
                if not self.sock:
                    return  # Socket closed by stop()
                data = self.sock.recv(slimproto.CTRL_RECV_SIZE)
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
                if elapsed > slimproto.SERVER_TIMEOUT_SEC:
                    log.info("No messages from server for %.0fs — connection dead, reconnecting", elapsed)
                    return
                continue
            except OSError:
                if not self.running:
                    return  # Socket closed by stop() during shutdown
                raise

            # Parse all complete messages from the accumulation buffer.
            # This is a two-state machine:
            #   expect_len is None → waiting for 2-byte length prefix
            #   expect_len is set  → waiting for that many payload bytes
            # After extracting a complete message, reset to None for next.
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
                expect_len = None                     # reset state for next message
                self._handle_message(msg)

    def _handle_message(self, msg):
        """Route a complete SlimProto message to the appropriate handler.

        Delegates to ProtocolHandler.dispatch() which owns all message parsing
        and handler logic. See protocol/handler.py for per-opcode details.
        """
        if len(msg) < 4:
            return
        log.debug("Received: %s (%d bytes)", msg[:4], len(msg))
        self.protocol.dispatch(msg)

    # --- Message handlers (delegated to protocol/handler.py) ---
    # All strm/audg/setd/aude/cont/serv/dsco handling lives in ProtocolHandler.
    # The handler calls back into methods below (_start_stream, _start_audio,
    # _resume_audio, _stop_playback, etc.) for state transitions.

    def _start_stream(self, stream_args):
        """Begin streaming a new track.

        Resets all per-track state (buffers, metadata, STAT flags, progress),
        spawns the status socket server (on first call), queries LMS for track
        metadata, and starts the stream worker thread.

        Called on fresh playback start or after a gapless track drain.
        """
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
        # For true gapless playback, increment track boundary
        self._current_track_id += 1
        self._track_start_frames = 0

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
            socket_path = os.path.expanduser(slimproto.STATUS_SOCKET_PATH)
            try:
                self._status_server = status_server.StatusSocketServer(self, socket_path)
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
                log.info("[%s] %s - %s%s",
                         album,
                         artist,
                         self.lms_title,
                         duration_str)

        except Exception as e:
            log.debug("LMS query failed: %s", e)
            # Clear "requesting..." state on error
            self.lms_title = ""

    def _lms_query(self, player_id, command, is_numeric=False):
        """Query LMS for a single metadata field. Delegates to lms_metadata module."""
        return lms_metadata.query_field(self.server_ip, player_id, command)

    def _lms_query_batch(self, player_id, fields):
        """Query LMS for multiple metadata fields. Delegates to lms_metadata module."""
        return lms_metadata.query_fields(self.server_ip, player_id, fields)

    def _parse_icy_metadata(self, data):
        """Parse ICY metadata block and update track info.

        Delegates to metadata.parse_icy_metadata() for the actual parsing,
        then updates instance state with the extracted title/artist/album.

        Args:
            data: Raw metadata block (bytes, starting with 1-byte length field)

        Returns:
            True if the title changed, False otherwise
        """
        result = metadata.parse_icy_metadata(data)
        title_changed = False

        if result["title"] and result["title"] != self.icy_title:
            self.icy_title = result["title"]
            log.info("Track: %s (from ICY metadata)", result["title"])
            title_changed = True
        if result["artist"]:
            self.icy_artist = result["artist"]
        if result["album"]:
            self.icy_album = result["album"]

        return title_changed

    def _status_dict(self):
        """Return current playback status as a dictionary for the status socket.

        Prefers LMS metadata (from JSON-RPC query) over ICY metadata (from stream).
        """
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
        """Stream worker thread — runs the entire HTTP stream lifecycle.

        Calls _do_stream() which handles the HTTP connection, then cleans up
        ffmpeg and sends DSCO (disconnect) to LMS when done. This runs in
        a daemon thread so it won't prevent shutdown.
        """
        try:
            self._do_stream(server_ip, server_port, http_header, threshold, autostart, fmt, pcm_info)
        except Exception as e:
            if self.streaming:
                log.warning("Stream error: %s", e)
        finally:
            # Wait for decode reader to finish — but only briefly.
            # _stop_playback() kills ffmpeg and closes the buffer first,
            # so the decode thread should exit quickly.
            if self.decode_thread and self.decode_thread.is_alive():
                self.decode_thread.join(timeout=2)
            self.streaming = False
            self._cleanup_ffmpeg()
            if self.running:
                try:
                    self._send(slimproto.build_dsco(0))
                except Exception:
                    pass

    def _do_stream(self, server_ip, server_port, http_header, threshold, autostart, fmt="?", pcm_info=None):
        """Connect to the stream server and route audio to the decoder pipeline.

        Handles the full HTTP stream setup:
        1. TCP connect (with SSL wrapping for HTTPS on port 443)
        2. Send the HTTP request that LMS gave us in the strm-s packet
        3. Read HTTP response headers, extract ICY metaint
        4. Forward headers to LMS (RESP packet) and confirm (STMc)
        5. Route audio data to either:
           - _stream_to_buffer() for PCM passthrough (native format, no ffmpeg)
           - _stream_to_ffmpeg() for compressed formats (via ffmpeg decode)
        """
        # Connect to stream server
        log.debug("Connecting to stream %s:%d", server_ip, server_port)
        self.stream_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.stream_sock.settimeout(slimproto.STREAM_CONNECT_TIMEOUT_SEC)
        self.stream_sock.connect((server_ip, server_port))

        # Wrap with SSL for HTTPS streams
        if server_port == slimproto.HTTPS_PORT:
            try:
                context = ssl.create_default_context()
                self.stream_sock = context.wrap_socket(
                    self.stream_sock,
                    server_hostname=server_ip
                )
                log.debug("SSL/TLS negotiated for HTTPS stream")
            except Exception as e:
                log.warning("SSL negotiation failed, continuing with HTTP: %s", e)

        # Send HTTP request
        self.stream_sock.sendall(http_header)

        # Read HTTP response headers
        resp_buf = bytearray()
        while b"\r\n\r\n" not in resp_buf:
            chunk = self.stream_sock.recv(slimproto.HTTP_HEADER_RECV_SIZE)
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
        self._send(slimproto.build_resp(resp_headers))
        self._send_stat("STMc")

        # For raw PCM at our native format, skip ffmpeg entirely
        pcm_passthrough = (fmt == "p" and pcm_info
                           and pcm_info["bits"] == 16 and pcm_info["endian"] == "le"
                           and pcm_info["rate"] == slimproto.SAMPLE_RATE
                           and pcm_info["channels"] == slimproto.CHANNELS)

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
                           "-f", "s16le", "-ar", str(self.next_sample_rate), "-ac", str(slimproto.CHANNELS),
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
        if not force and avail < max(threshold, slimproto.MIN_THRESHOLD_BYTES):
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

        self.stream_sock.settimeout(slimproto.STREAM_READ_TIMEOUT_SEC)
        while self.streaming and self.running:
            try:
                data = self.stream_sock.recv(slimproto.STREAM_RECV_SIZE)
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

        self.stream_sock.settimeout(slimproto.STREAM_READ_TIMEOUT_SEC)
        while self.streaming and self.running:
            try:
                chunk = self.stream_sock.recv(slimproto.STREAM_RECV_SIZE)
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
                data = self.ffmpeg_proc.stdout.read(slimproto.FFMPEG_READ_SIZE)
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
        """Terminate the ffmpeg subprocess and close all its pipes.

        Order matters: close stdin first (signals EOF to ffmpeg), then kill,
        then close stdout (unblocks any thread doing stdout.read()), then wait.
        """
        if self.ffmpeg_proc:
            try:
                self.ffmpeg_proc.stdin.close()
            except Exception:
                pass
            try:
                self.ffmpeg_proc.kill()
            except Exception:
                pass
            # Close stdout to unblock any thread blocked on stdout.read()
            try:
                self.ffmpeg_proc.stdout.close()
            except Exception:
                pass
            try:
                self.ffmpeg_proc.wait(timeout=2)
            except Exception:
                pass
            self.ffmpeg_proc = None

    # --- Audio output ---

    def _build_fade_curves(self, fade_duration_samples):
        """Build linear gain curves for fade in/out.

        Returns tuple: (fade_in_gains, fade_out_gains)
        Each is a list of normalized gains [0.0-1.0] for each sample position.
        fade_in: 0.0 → 1.0 (silent to full)
        fade_out: 1.0 → 0.0 (full to silent)
        """
        if fade_duration_samples <= 0:
            return None, None

        fade_in = [i / fade_duration_samples for i in range(fade_duration_samples)]
        fade_out = [1.0 - g for g in fade_in]  # Complementary: 1.0 → 0.0

        return fade_in, fade_out

    def _apply_crossfade(self, new_chunk):
        """Mix old and new track samples during crossfade window.

        Implements 5 fade modes:
        - 0: FADE_NONE (immediate switch, no fade)
        - 1: CROSSFADE (old fades out, new fades in)
        - 2: FADE_IN (new fades in)
        - 3: FADE_OUT (old fades out)
        - 4: FADE_INOUT (both fade simultaneously)

        Returns mixed chunk with old track fading out, new track fading in.
        """
        if not self._crossfade_samples:
            return new_chunk

        old_samples = array.array("h", bytes(self._crossfade_samples[:len(new_chunk)]))
        new_samples = array.array("h", new_chunk)
        mixed = array.array("h")

        for i in range(min(len(old_samples), len(new_samples))):
            # Calculate position in the crossfade window (i is sample index)
            pos_in_fade = self._crossfade_pos + i

            if pos_in_fade < self._crossfade_total:
                # Still in fade window — apply gain curves
                if self.transition_type == 1:  # CROSSFADE
                    gain_out = self._fade_out_gains[pos_in_fade]
                    gain_in = self._fade_in_gains[pos_in_fade]
                elif self.transition_type == 2:  # FADE_IN
                    gain_out = 0.0
                    gain_in = self._fade_in_gains[pos_in_fade]
                elif self.transition_type == 3:  # FADE_OUT
                    gain_out = self._fade_out_gains[pos_in_fade]
                    gain_in = 0.0
                elif self.transition_type == 4:  # FADE_INOUT
                    gain_out = self._fade_out_gains[pos_in_fade]
                    gain_in = self._fade_in_gains[pos_in_fade]
                else:  # FADE_NONE
                    gain_out = 0.0
                    gain_in = 1.0

                # Mix: old_sample × gain_out + new_sample × gain_in
                mixed_sample = int(old_samples[i] * gain_out + new_samples[i] * gain_in)
                mixed.append(mixed_sample)
            else:
                # Crossfade window finished, use new sample only
                mixed.append(new_samples[i])

        # Update position for next batch of samples
        self._crossfade_pos += len(new_samples)

        if self._crossfade_pos >= self._crossfade_total:
            log.debug("Crossfade complete after %d samples", self._crossfade_total)
            self._crossfade_enabled = False

        return mixed.tobytes()

    def _reset_track_state(self):
        """Reset state for a new track (true gapless).
        Called when switching tracks without closing the device."""
        self._current_track_id += 1
        self._track_start_frames = self.output_frames
        self.decode_complete = False
        self.sent_STMd = False
        self.sent_STMu = False
        self.sent_STMo = False
        self.sent_STMl = False
        self._switching_track = False
        # Clear crossfade state at track boundary
        self._crossfade_enabled = False
        self._crossfade_samples.clear()
        self._crossfade_pos = 0
        self._crossfade_total = 0
        log.debug("Track boundary: switching to track #%d at frame %d",
                  self._current_track_id, self._track_start_frames)

    def _audio_generator(self):
        """Generator that yields PCM data to the miniaudio playback device.

        miniaudio calls send(framecount) on this generator from its callback
        thread, and we yield exactly framecount * slimproto.BYTES_PER_FRAME bytes back.

        This generator is the heart of the audio pipeline. It handles:
        - Silence during pause and sync-wait periods
        - Reading PCM data from the buffer and tracking elapsed frames
        - Accumulating tail samples for crossfade at track boundaries
        - Initializing and applying crossfade mixing between tracks
        - Volume and replay gain scaling
        - STMd/STMu signaling when tracks complete
        - Gapless track transitions (starting new stream without closing device)
        - Buffer underrun detection (STMo)

        IMPORTANT: self.playing must be True BEFORE device.start(gen) because
        on Linux the miniaudio callback fires immediately and this generator
        checks self.playing before yielding any data.
        """
        required_frames = yield b""  # priming yield
        while self.playing and self.running:
            if self.paused:
                required_frames = yield b"\x00" * (required_frames * slimproto.BYTES_PER_FRAME)
                continue

            required_bytes = required_frames * slimproto.BYTES_PER_FRAME

            # Sync: if start_at_jiffies is set, output silence until target time
            # (like squeezelite's OUTPUT_START_AT state)
            if self.start_at_jiffies:
                now = slimproto.gettime_ms()
                # 32-bit unsigned subtraction with wrap-around handling:
                # - Mask to u32 range (& 0xFFFFFFFF)
                # - diff < JIFFIES_WRAP_GUARD: target is in the future (not wrapped past)
                # - diff > 0: target hasn't been reached yet
                # - diff < SYNC_START_WINDOW_MS: target is within a reasonable window
                #   (avoids waiting forever on stale or bogus timestamps)
                diff = (self.start_at_jiffies - now) & 0xFFFFFFFF
                if (diff < slimproto.JIFFIES_WRAP_GUARD
                        and diff > 0
                        and diff < slimproto.SYNC_START_WINDOW_MS):
                    required_frames = yield b"\x00" * required_bytes
                    continue
                # Target reached or passed — clear and start real audio
                log.debug("Sync target reached (target=%d now=%d) — starting audio",
                          self.start_at_jiffies, now)
                self.start_at_jiffies = 0
                # Reset output_frames so elapsed time starts from zero at the
                # moment real audio begins — the silence frames don't count.
                self.output_frames = 0
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
                    self.output_frames += len(chunk) // slimproto.BYTES_PER_FRAME
                    # Crossfade tail accumulation: while the old track is draining
                    # (decode done, next track queued), save the last N seconds of
                    # audio. This becomes the "old side" of the crossfade mix when
                    # the new track starts. We only accumulate when all three
                    # conditions are true:
                    #   - decode_complete: old track's decoder has finished
                    #   - transition_type > 0: LMS requested a fade transition
                    #   - _pending_track: next track is queued and waiting
                    if self.decode_complete and self.transition_type > 0 and self._pending_track:
                        self._crossfade_samples.extend(chunk)
                        # Cap to transition_period worth of audio (sliding window)
                        max_bytes = int(self.transition_period_sec * self.current_sample_rate * slimproto.BYTES_PER_FRAME)
                        if len(self._crossfade_samples) > max_bytes:
                            self._crossfade_samples = self._crossfade_samples[-max_bytes:]
                    if len(chunk) < required_bytes:
                        chunk += b"\x00" * (required_bytes - len(chunk))

                    # Crossfade initialization gate: this fires on the first chunk
                    # of a new track after _reset_track_state() clears _crossfade_enabled.
                    # It requires _crossfade_samples to be non-empty (populated during
                    # the old track's drain phase above). The timing is: old track drains
                    # → tail samples accumulated → gapless switch → first new chunk
                    # arrives here → crossfade curves are built and mixing begins.
                    if self.transition_type > 0 and not self._crossfade_enabled:
                        fade_duration_samples = int(self.transition_period_sec * self.current_sample_rate)
                        if fade_duration_samples > 0 and len(self._crossfade_samples) > 0:
                            self._crossfade_enabled = True
                            self._crossfade_total = fade_duration_samples
                            self._fade_in_gains, self._fade_out_gains = self._build_fade_curves(fade_duration_samples)
                            log.debug("Starting crossfade: %d samples, type=%d", fade_duration_samples, self.transition_type)

                    # Apply crossfade mixing if active
                    if self._crossfade_enabled and self._crossfade_pos < self._crossfade_total:
                        chunk = self._apply_crossfade(chunk)

                    # Apply volume scaling: remote control (audg) × replay gain (strm s)
                    # Both are normalized to 1.0 = unity gain
                    vol = self.volume * self.replay_gain
                    if vol < 0.999:
                        samples = array.array("h", chunk)
                        for i in range(len(samples)):
                            samples[i] = int(samples[i] * vol)
                        chunk = samples.tobytes()
                    required_frames = yield chunk
                    continue

            # Buffer empty — bail immediately during shutdown
            if not self.running:
                break
            if self.decode_complete and avail == 0:
                # Send STMd once when buffer is fully drained (not when decode
                # finishes). Sending early causes LMS to send the next strm-s
                # which kills buffered audio.
                if not self.sent_STMd:
                    self.sent_STMd = True
                    self._send_stat("STMd")
                # Track fully played — check for gapless transition
                if self._pending_track:
                    # True gapless: switch to next track without closing device
                    log.debug("Track complete — gapless switch to pending track")
                    # Crossfade samples were accumulated during playback (above)
                    if self._crossfade_samples:
                        log.debug("Crossfade: %d bytes from old track tail", len(self._crossfade_samples))
                    self._send_stat("STMu")  # Send output underrun for current track
                    self._reset_track_state()
                    # Start the new stream (network connection only, device stays open)
                    server_ip, server_port, http_header, threshold, autostart, fmt, pcm_info = self._pending_track
                    self._pending_track = None
                    self.streaming = True
                    self.stream_bytes = 0
                    self.cont_received = (autostart < 2)
                    self.autostart = autostart
                    # Start stream thread for new track (device already running)
                    self.stream_thread = threading.Thread(
                        target=self._stream_worker,
                        args=(server_ip, server_port, http_header, threshold, autostart, fmt, pcm_info),
                        daemon=True,
                    )
                    self.stream_thread.start()
                    # Loop continues, waiting for new data
                    required_frames = yield b"\x00" * required_bytes
                    continue
                else:
                    # No pending track — playback finished
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

        # Generator exiting — playback stopped (no pending track)
        self.playing = False
        log.info("Playback finished")

    def _start_audio(self, sample_rate=None):
        """Create a miniaudio playback device and start the audio generator.

        Opens the audio device at the requested sample rate, primes the
        generator (first yield returns empty bytes), and starts playback.
        self.playing is set True BEFORE device.start() — this is critical
        because on Linux the callback fires immediately.
        """
        if self.playing:
            return

        # Determine the sample rate to use (variable sample rate support)
        rate = sample_rate or self.next_sample_rate
        self.current_sample_rate = self._get_supported_rate(rate)
        log.info("Starting audio playback at %d Hz", self.current_sample_rate)
        try:
            self.device = miniaudio.PlaybackDevice(
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=slimproto.CHANNELS,
                sample_rate=self.current_sample_rate,
                buffersize_msec=slimproto.DEVICE_BUFFER_MSEC,
                device_id=self.audio_device_id,
            )
        except Exception as e:
            if self.audio_device_id is not None:
                # Device-specific failure (e.g., ALSA "device busy") — retry with system default
                log.warning("Audio device failed (%s), retrying with system default", e)
                try:
                    self.device = miniaudio.PlaybackDevice(
                        output_format=miniaudio.SampleFormat.SIGNED16,
                        nchannels=slimproto.CHANNELS,
                        sample_rate=self.current_sample_rate,
                        buffersize_msec=slimproto.DEVICE_BUFFER_MSEC,
                    )
                except Exception as e2:
                    log.error("Audio start failed (default device): %s", e2)
                    return
            else:
                log.error("Audio start failed: %s", e)
                return
        try:
            log.debug("Audio device buffer: %dms (requested %dms)",
                      self.device.buffersize_msec, slimproto.DEVICE_BUFFER_MSEC)
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
        log.debug("Sync start at jiffies=%d (now=%d)", self.start_at_jiffies, slimproto.gettime_ms())
        self._start_audio()

    def _resume_audio(self, sample_rate=None):
        """Resume audio after pause by closing the old device and creating a new one.

        We can't simply unpause miniaudio — we need a fresh device and generator
        because the old generator's state is stale. Dynamic delay tracking is also
        reset because the old wall-clock reference is no longer valid after pause.
        """
        if not self.playing:
            self._start_audio(sample_rate)
            return

        # Determine the sample rate to use (variable sample rate support)
        rate = sample_rate or self.next_sample_rate
        self.current_sample_rate = self._get_supported_rate(rate)
        log.info("Resuming audio at %d Hz (%d bytes buffered)",
                 self.current_sample_rate, self.pcm_buf.available())
        # Close old device before creating new one
        if self.device:
            try:
                self.device.close()
            except Exception:
                pass
            self.device = None
        # Reset dynamic delay tracking — the old wall-clock reference is stale
        # after pause. Without this, _elapsed_ms() over-reports because
        # ms_since (wall time since old device start) is much larger than
        # frames_since (frames yielded by new generator), making buffer_ms
        # negative (clamped to 0) and device_delay_frames too small.
        self._device_start_time = None
        self._device_start_frames = self.output_frames
        try:
            self.device = miniaudio.PlaybackDevice(
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=slimproto.CHANNELS,
                sample_rate=self.current_sample_rate,
                buffersize_msec=slimproto.DEVICE_BUFFER_MSEC,
                device_id=self.audio_device_id,
            )
        except Exception as e:
            if self.audio_device_id is not None:
                log.warning("Audio device failed on resume (%s), retrying with system default", e)
                try:
                    self.device = miniaudio.PlaybackDevice(
                        output_format=miniaudio.SampleFormat.SIGNED16,
                        nchannels=slimproto.CHANNELS,
                        sample_rate=self.current_sample_rate,
                        buffersize_msec=slimproto.DEVICE_BUFFER_MSEC,
                    )
                except Exception as e2:
                    log.error("Audio resume failed (default device): %s", e2)
                    return
            else:
                log.error("Audio resume failed: %s", e)
                return
        try:
            gen = self._audio_generator()
            next(gen)  # prime the generator
            self.device.start(gen)
        except Exception as e:
            log.error("Audio resume failed: %s", e)

    def _stop_playback(self):
        """Stop all playback and clean up threads/resources.

        Teardown order is critical to avoid deadlocks:
        1. Signal all loops to stop (streaming=False, playing=False)
        2. Close PCM buffer (unblocks decode thread stuck on buf.write())
        3. Close stream socket (unblocks stream thread stuck on recv())
        4. Kill ffmpeg (unblocks decode thread on stdout.read(),
           stream thread on stdin.write())
        5. Join decode thread (should be unblocked now)
        6. Join stream thread (it also joins decode, but it's already done)
        7. Close audio device LAST (generator exits via playing=False)
        8. Flush PCM buffer for reuse
        """
        self.streaming = False
        self.playing = False
        self.paused = False
        self.output_frames = 0
        self.decode_complete = False
        self._pending_track = None
        self._device_start_time = None
        self._device_start_frames = 0

        # Unblock writers FIRST — the decode thread may be blocked on
        # pcm_buf.write() with the buffer full and no reader consuming.
        self.pcm_buf.close()

        # Close stream socket EARLY to unblock stream thread's recv()
        if self.stream_sock:
            try:
                self.stream_sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self.stream_sock.close()
            except Exception:
                pass
            self.stream_sock = None

        # Kill ffmpeg and close its stdout — unblocks decode thread's
        # ffmpeg.stdout.read() and stream thread's stdin.write()
        self._cleanup_ffmpeg()

        # Wait for decode thread FIRST (it should be unblocked now)
        if self.decode_thread and self.decode_thread.is_alive():
            self.decode_thread.join(timeout=2)
        self.decode_thread = None

        # Wait for stream thread (it also joins decode thread, but it's already done)
        if self.stream_thread and self.stream_thread.is_alive():
            self.stream_thread.join(timeout=2)
        self.stream_thread = None

        # Stop the audio device LAST — the generator checks self.running and
        # exits immediately during shutdown (no complex cleanup).
        # All other threads are stopped so no lock contention on pcm_buf.
        if self.device:
            try:
                self.device.close()
            except Exception:
                pass
            self.device = None
        self.cont_received = False
        self.sent_STMd = False
        self.sent_STMu = False
        self.sent_STMo = False
        self.sent_STMl = False

        self.pcm_buf.flush()

    def stop(self):
        """Signal the player to shut down.

        Called from signal handler — must be safe to run mid-recv().
        Only sets the flag and closes the socket to unblock recv().
        Actual cleanup happens in run()'s finally block.
        """
        if not self.running:
            return
        log.info("Shutting down...")
        self.running = False
        # Close the TCP socket to unblock _message_loop's recv() immediately.
        # This causes recv() to raise an exception, which exits _message_loop,
        # and run()'s finally block handles the full cleanup.
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass


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
    parser.add_argument("-n", "--name", default=None, help="Player name (default: saved name or 'Squeezy')")
    parser.add_argument("-m", "--mac", help="MAC address aa:bb:cc:dd:ee:ff (auto-detect if not set)")
    parser.add_argument("-d", "--device", help="Audio output device (name or substring, e.g. 'HDMI')")
    parser.add_argument("-l", "--list-devices", action="store_true", help="List audio output devices and exit")
    parser.add_argument("--latency", type=int, default=None, metavar="MS",
                        help=f"OS audio pipeline latency in ms (default: {slimproto.PLATFORM_PIPELINE_MSEC}ms on this platform). "
                             "Increase if sync is behind, decrease if ahead.")
    parser.add_argument("--buffer-size", type=int, default=None, metavar="KB",
                        help="PCM buffer size in KB (default: 4096, range: 64-8192). "
                             "Larger buffers help on slow networks, smaller reduces latency.")
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
                     device_id=device_id, latency_msec=args.latency,
                     buffer_size_kb=args.buffer_size)

    def handle_signal(sig, frame):
        if not player.running:
            # Second signal — force exit immediately
            import os
            os._exit(1)
        player.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    player.run()


if __name__ == "__main__":
    main()

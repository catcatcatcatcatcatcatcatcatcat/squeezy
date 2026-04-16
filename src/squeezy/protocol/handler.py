#!/usr/bin/env python3
"""SlimProto message dispatch and packet parsing.

Routes incoming LMS messages to the appropriate handler based on the 4-byte
ASCII opcode. Each handler extracts opcode-specific fields from the raw
message bytes and triggers state transitions on the Squeezy instance.

This module owns all message parsing logic so that squeezy.py only needs
to call ``self.protocol.dispatch(msg)`` from its message loop.

Supported opcodes:
    strm — Stream control (start, pause, unpause, quit, flush, skip, timing)
    audg — Volume/gain control from LMS remote
    setd — Get/set player data (player name)
    aude — Audio enable/disable (no-op; codec support is static)
    cont — Sync group continuation signal
    serv — Server redirect
    dsco — Server-initiated disconnect
"""

import logging
import socket
import struct

from . import slimproto

log = logging.getLogger("squeezy")


class ProtocolHandler:
    """Routes and handles incoming SlimProto protocol messages from LMS.

    Each handler method parses the opcode-specific fields from the raw message
    bytes and triggers the appropriate state transitions on the Squeezy instance.
    This class holds no state of its own — all player state lives on self.squeezy.
    """

    def __init__(self, squeezy_ref):
        """Initialize protocol handler.

        Args:
            squeezy_ref: Reference to the Squeezy player instance. All handler
                         methods read/write state via this reference.
        """
        self.squeezy = squeezy_ref

    def dispatch(self, msg):
        """Route a SlimProto message to the appropriate handler by opcode.

        Unknown opcodes are logged at debug level and silently ignored —
        LMS may send opcodes for features we don't implement yet.

        Args:
            msg: Complete SlimProto message bytes (4-byte opcode + payload).
        """
        if len(msg) < 4:
            return

        opcode = msg[:4]

        handlers = {
            b"strm": self._handle_strm,
            b"audg": self._handle_audg,
            b"setd": self._handle_setd,
            b"aude": self._handle_aude,
            b"cont": self._handle_cont,
            b"serv": self._handle_serv,
            b"dsco": self._handle_dsco,
        }
        handler = handlers.get(opcode)
        if handler:
            handler(msg)
        else:
            log.debug("Unhandled opcode: %s", opcode)

    # --- STRM: Stream control ---

    def _handle_strm(self, msg):
        """Handle STRM message — the main stream control command from LMS.

        Dispatches to sub-handlers based on the single-character subcommand:
            's' = start stream     'p' = pause        'u' = unpause
            'q' = quit             'f' = flush         'a' = skip ahead
            't' = timing request (heartbeat echo)
        """
        if len(msg) < 5:
            return
        command = chr(msg[4])
        log.debug("strm command: %s", command)

        if command == "t":
            # Timing request — echo server timestamp back to LMS.
            # LMS uses the round-trip time to estimate network delay for sync.
            if len(msg) >= 22:
                ts = struct.unpack_from(">I", msg, 18)[0]
                self.squeezy._send_stat("STMt", server_timestamp=ts)
            else:
                self.squeezy._send_stat("STMt")

        elif command == "s":
            self._handle_strm_start(msg)

        elif command == "p":
            # Pause — replay_gain field = interval in ms (0 = immediate).
            # We treat all pauses as immediate (squeezelite does the same).
            interval = 0
            if len(msg) >= 22:
                interval = struct.unpack_from(">I", msg, 18)[0]
            if interval:
                log.debug("Pause with interval %d ms (treating as immediate)", interval)
            if self.squeezy.playing and not self.squeezy.paused:
                self.squeezy.paused = True
                if self.squeezy.device:
                    try:
                        self.squeezy.device.close()
                    except Exception:
                        pass
                    self.squeezy.device = None
            # Always confirm pause to LMS (squeezelite sends STMp regardless of interval)
            self.squeezy._send_stat("STMp")

        elif command == "u":
            # Unpause with optional sync timestamp (used for multi-room sync).
            # Like squeezelite: if jiffies is non-zero, enter start-at-time
            # mode (play silence until target jiffies reached, then start).
            # If zero, start immediately.
            target_jiffies = 0
            if len(msg) >= 22:
                target_jiffies = struct.unpack_from(">I", msg, 18)[0]

            self.squeezy.start_at_jiffies = target_jiffies
            log.debug("unpause at: %d now: %d", target_jiffies, slimproto.gettime_ms())
            if self.squeezy.paused:
                self.squeezy.paused = False
                self.squeezy._resume_audio()
            elif not self.squeezy.playing and self.squeezy.pcm_buf.available() > 0:
                # Not yet playing (e.g., sync mode: we sent STMl but LMS
                # hadn't told us to start yet). Start audio now — the
                # generator will output silence until start_at_jiffies.
                if target_jiffies:
                    self.squeezy._start_audio_at_time()
                else:
                    self.squeezy._start_audio()
            self.squeezy._send_stat("STMr")

        elif command == "a":
            # Skip ahead — replay_gain field = milliseconds to skip
            if len(msg) >= 22:
                skip_ms = struct.unpack_from(">I", msg, 18)[0]
                skip_frames = int(skip_ms * self.squeezy.current_sample_rate / 1000)
                skip_bytes = skip_frames * slimproto.BYTES_PER_FRAME
                actual = self.squeezy.pcm_buf.skip(skip_bytes)
                skipped_frames = actual // slimproto.BYTES_PER_FRAME
                self.squeezy.output_frames += skipped_frames
                log.debug("Skip ahead: %d ms (%d frames requested, %d skipped)",
                         skip_ms, skip_frames, skipped_frames)
            self.squeezy._send_stat("STMc")

        elif command == "q":
            # Quit streaming entirely — hard stop, always report completion.
            # This command tells the player to stop immediately and disconnect.
            log.debug("Quit command: stopping playback and stream")
            self.squeezy._stop_playback()
            self.squeezy._send_stat("STMf")

        elif command == "f":
            # Flush output buffer — graceful stop that may allow track queuing.
            # Only send STMf if we were actually playing/streaming.
            was_active = self.squeezy.streaming or self.squeezy.playing
            log.debug("Flush command: stopping current playback (was_active=%s)", was_active)
            self.squeezy._stop_playback()
            if was_active:
                self.squeezy._send_stat("STMf")

    # --- STRM 's': Start stream ---

    def _handle_strm_start(self, msg):
        """Handle 'strm s' — start a new audio stream.

        Parses the packet fields (codec, sample rate, threshold, replay gain,
        crossfade params, server address, HTTP header), then either queues
        the track for gapless transition or starts a new stream immediately.

        Packet layout (all offsets relative to start of message):

          off  len  field
           0    4   opcode "strm"
           4    1   command 's'
           5    1   autostart  ASCII digit: '0'=immediate, '1'=output-buffer,
                                '2'=wait-for-CONT, '3'=wait-for-CONT+output-buffer
           6    1   format     codec: 'm'=mp3, 'f'=flac, 'p'=pcm, 'o'=ogg,
                                'a'=aac, 'w'=wma, 'l'=alac, 'e'=aac-he
           7    1   pcm_sample_size  ASCII: '0'=8, '1'=16, '2'=20, '3'=24, '4'=32
           8    1   pcm_sample_rate  ASCII digit index into rate table
           9    1   pcm_channels     ASCII: '1'=mono, '2'=stereo
          10    1   pcm_endianness   ASCII: '0'=big-endian, '1'=little-endian
          11    1   threshold  output buffer threshold in 1KB units (threshold*1024)
          12    1   spdif_enable
          13    1   transition_period  crossfade seconds
          14    1   transition_type    0=none,1=crossfade,2=fade-in,3=fade-out,4=in+out
          15    1   flags
          16    1   output_threshold
          17    1   slaves (sync)
          18    4   replay_gain  (u32, fixed-point 16.16 — 0x10000 = unity)
          22    2   server_port  (u16be)
          24    4   server_ip    (u32be, 0 = use slimproto server address)
          28    …   http_header  raw HTTP request bytes to send to stream server
        """
        if len(msg) < 28:
            log.warning("strm 's' packet too short")
            return

        self.squeezy.autostart = msg[5] - ord("0") if msg[5] >= ord("0") else 0
        fmt = chr(msg[6])
        pcm_sample_size = msg[7]
        pcm_sample_rate = msg[8]
        pcm_channels = msg[9]
        pcm_endian = msg[10]
        threshold = msg[11] * 1024

        # Extract replay_gain (16.16 fixed-point at offset 18).
        # Raw value 0 means "no replay gain" → use unity (1.0).
        if len(msg) >= 22:
            replay_gain_raw = struct.unpack_from(">I", msg, 18)[0]
            self.squeezy.replay_gain = (replay_gain_raw / slimproto.GAIN_FIXED_POINT_ONE
                                        if replay_gain_raw else 1.0)
        else:
            self.squeezy.replay_gain = 1.0  # Default if packet too short

        # Extract transition parameters (offsets 13-14) for crossfade support.
        # Convert ASCII digits to integers (bytes 0x30-0x39 map to 0-9).
        if len(msg) >= 15:
            transition_period_raw = msg[13]
            transition_type_raw = msg[14]
            self.squeezy.transition_period_sec = (transition_period_raw - ord("0")
                                                  if transition_period_raw >= ord("0") else 0)
            self.squeezy.transition_type = (transition_type_raw - ord("0")
                                            if transition_type_raw >= ord("0") else 0)
        else:
            self.squeezy.transition_type = 0
            self.squeezy.transition_period_sec = 0
        log.debug("Transition: type=%d period=%ds",
                  self.squeezy.transition_type, self.squeezy.transition_period_sec)

        server_port = struct.unpack_from(">H", msg, 22)[0]
        server_ip_raw = struct.unpack_from(">I", msg, 24)[0]
        http_header = msg[28:]

        # server_ip == 0 means "same host as the LMS slimproto connection"
        if server_ip_raw == 0:
            server_ip = self.squeezy.server_ip
        else:
            server_ip = socket.inet_ntoa(struct.pack(">I", server_ip_raw))

        # PCM format fields use ASCII digit encoding from squeezelite's pcm.c.
        # They're only meaningful when fmt == 'p' (raw PCM); for compressed
        # formats (mp3, flac, etc.) ffmpeg auto-detects from the stream.
        pcm_info = None
        if fmt == "p":
            bits = slimproto.PCM_SAMPLE_SIZE_MAP.get(pcm_sample_size, 16)
            rate_idx = pcm_sample_rate - ord("0") if pcm_sample_rate >= ord("0") else 0
            rate = (slimproto.PCM_RATE_TABLE[rate_idx]
                    if rate_idx < len(slimproto.PCM_RATE_TABLE)
                    else slimproto.SAMPLE_RATE)
            chans = pcm_channels - ord("0") if pcm_channels >= ord("0") else 2
            if chans not in (1, 2):
                chans = 2
            endian = "le" if pcm_endian == ord("1") else "be"
            pcm_info = {"bits": bits, "rate": rate, "channels": chans, "endian": endian}

        # Detect sample rate for this stream (variable sample rate support)
        if fmt == "p" and pcm_info:
            self.squeezy.next_sample_rate = self.squeezy._get_supported_rate(pcm_info["rate"])
        else:
            # For compressed formats, will detect from ffmpeg output later
            self.squeezy.next_sample_rate = 44100  # Default, may be updated by ffmpeg detection

        log.debug("Stream start: format=%s server=%s:%d threshold=%d autostart=%d "
                  "replay_gain=%.2f pcm=%s",
                  fmt, server_ip, server_port, threshold, self.squeezy.autostart,
                  self.squeezy.replay_gain, pcm_info)

        stream_args = (server_ip, server_port, http_header, threshold,
                       self.squeezy.autostart, fmt, pcm_info)

        # Like squeezelite: if audio is still playing from the previous track
        # (decode done, buffer draining), don't kill the output — queue the
        # next track and let the current one finish.
        if self.squeezy.playing and self.squeezy.decode_complete:
            log.debug("Track still playing — queuing next track for gapless transition")
            self.squeezy._pending_track = stream_args
            self.squeezy._send_stat("STMf")
            # Stop the old *stream* (network) but keep the audio device running
            self.squeezy.streaming = False
            if self.squeezy.stream_sock:
                try:
                    self.squeezy.stream_sock.close()
                except Exception:
                    pass
            return

        # Stop any existing playback
        self.squeezy._stop_playback()
        self.squeezy._send_stat("STMf")
        self.squeezy._start_stream(stream_args)

    # --- AUDG: Volume control ---

    def _handle_audg(self, msg):
        """Handle AUDG message — volume control from LMS remote.

        The gain value is a 16.16 fixed-point number where 0x10000 = 1.0 (unity).
        We use the left channel gain as a mono volume multiplier, capped at 1.0.
        Only applied when the 'adjust' flag (byte 12) is set.
        """
        if len(msg) >= 22:
            adjust = msg[12]
            gain_l = struct.unpack_from(">I", msg, 14)[0]
            gain_r = struct.unpack_from(">I", msg, 18)[0]
            # Use left channel gain as volume (mono mix); max gain is GAIN_FIXED_POINT_ONE
            self.squeezy.volume = (min(gain_l / slimproto.GAIN_FIXED_POINT_ONE, 1.0)
                                   if adjust else 1.0)
            log.debug("Volume: %.0f%% (L=%.2f R=%.2f adjust=%d)",
                      self.squeezy.volume * 100,
                      gain_l / slimproto.GAIN_FIXED_POINT_ONE,
                      gain_r / slimproto.GAIN_FIXED_POINT_ONE, adjust)

    # --- SETD: Player data ---

    def _handle_setd(self, msg):
        """Handle SETD message — get/set player data (currently just player name).

        When LMS sends SETD with id=0 and no payload, it's a query — we respond
        with our current name. When it includes a payload, it's a set — we update
        the name and persist it to disk.
        """
        if len(msg) < 5:
            return
        setd_id = msg[4]
        if setd_id == slimproto.SETD_ID_PLAYER_NAME:
            if len(msg) == 5:
                # Query player name — respond with null-terminated UTF-8
                name_data = self.squeezy.name.encode("utf-8") + b"\x00"
                self.squeezy._send(slimproto.build_setd(slimproto.SETD_ID_PLAYER_NAME, name_data))
            elif len(msg) > 5:
                # Set player name — update and persist to XDG config
                new_name = msg[5:].rstrip(b"\x00").decode("utf-8", errors="replace")
                if new_name:
                    self.squeezy.name = new_name
                    self.squeezy._save_player_name(new_name)
                    log.info("Player name set to: %s", self.squeezy.name)
                name_data = self.squeezy.name.encode("utf-8") + b"\x00"
                self.squeezy._send(slimproto.build_setd(slimproto.SETD_ID_PLAYER_NAME, name_data))

    # --- AUDE: Audio enable/disable ---

    def _handle_aude(self, msg):
        """Handle AUDE message — audio enable/disable (no-op).

        LMS sends this to enable/disable audio codecs. We don't need to act
        on it since our codec support is static (determined at startup by
        probing ffmpeg).
        """
        log.debug("aude received")

    # --- CONT: Sync continuation ---

    def _handle_cont(self, msg):
        """Handle CONT (continuation) packet for sync group playback and metaint updates.

        CONT packet format (from squeezelite slimproto.c:399-415):
        - Used for synchronized playback (autostart >= 2)
        - May include metaint field for ICY metadata interval
        """
        log.debug("cont received (autostart was %d)", self.squeezy.autostart)
        if self.squeezy.autostart >= 2:
            self.squeezy.autostart -= 2
            self.squeezy.cont_received = True

        # Extract metaint from CONT packet if present (for ICY metadata support)
        # CONT packet may include metaint at offset 4 (u32 big-endian)
        if len(msg) >= 8:
            metaint = struct.unpack_from(">I", msg, 4)[0]
            if metaint > 0:
                self.squeezy.icy_meta_int = metaint
                log.debug("CONT metaint updated to %d bytes", metaint)

    # --- SERV: Server redirect ---

    def _handle_serv(self, msg):
        """Handle SERV message — server redirect.

        LMS sends this when migrating players between server instances. We
        update our server_ip so the next reconnect goes to the new server.
        """
        if len(msg) >= 8:
            new_ip = struct.unpack_from(">I", msg, 4)[0]
            if new_ip:
                self.squeezy.server_ip = socket.inet_ntoa(struct.pack(">I", new_ip))
                log.info("Server redirect to %s", self.squeezy.server_ip)

    # --- DSCO: Server-initiated disconnect ---

    def _handle_dsco(self, msg):
        """Handle DSCO message — server-initiated disconnect.

        LMS sends this during server maintenance or when a player is
        registered to two servers. We stop playback and break out of the
        message loop by closing the socket (squeezelite does the same in
        slimproto.c:361-370).
        """
        log.info("Server sent DSCO — disconnecting for reconnect")
        self.squeezy._stop_playback()
        # Close socket to break out of _message_loop recv()
        if self.squeezy.sock:
            try:
                self.squeezy.sock.close()
            except Exception:
                pass
            self.squeezy.sock = None

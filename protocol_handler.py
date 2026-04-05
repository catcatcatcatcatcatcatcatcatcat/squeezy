#!/usr/bin/env python3
"""SlimProto message dispatch and packet parsing.

Routes incoming LMS messages to handlers, extracts packet parameters,
and triggers appropriate Squeezy state transitions.
"""

import logging
import socket
import struct

log = logging.getLogger("squeezy")


class ProtocolHandler:
    """Routes and handles SlimProto protocol messages."""

    def __init__(self, squeezy_ref):
        """Initialize protocol handler.

        Args:
            squeezy_ref: Reference to Squeezy instance
        """
        self.squeezy = squeezy_ref

    def dispatch(self, msg):
        """Dispatch incoming message to appropriate handler.

        Args:
            msg: Complete SlimProto message (opcode + payload)
        """
        if len(msg) < 5:
            return

        opcode = msg[0:4]

        if opcode == b"strm":
            self.handle_strm(msg)
        elif opcode == b"audg":
            self.handle_audg(msg)
        elif opcode == b"setd":
            self.handle_setd(msg)
        elif opcode == b"aude":
            self.handle_aude(msg)
        elif opcode == b"cont":
            self.handle_cont(msg)
        elif opcode == b"serv":
            self.handle_serv(msg)
        else:
            log.debug("Unknown opcode: %s", opcode)

    def handle_strm(self, msg):
        """Handle STRM message — stream control."""
        if len(msg) < 5:
            return

        subcommand = chr(msg[4])
        log.debug("Handling strm subcommand: %s (message len=%d)", subcommand, len(msg))

        if subcommand == "t":
            # Timing request — extract server timestamp from message and echo it back
            if len(msg) >= 22:
                ts = struct.unpack_from(">I", msg, 18)[0]
                self.squeezy.server_timestamp = ts
                self.squeezy._send_stat("STMt", server_timestamp=ts)
            else:
                self.squeezy._send_stat("STMt")
        elif subcommand == "s":
            # Stream start
            self.handle_strm_start(msg)
        elif subcommand == "p":
            # Pause — close audio device (matches squeezelite)
            if self.squeezy.playing and not self.squeezy.paused:
                self.squeezy.paused = True
                if self.squeezy.device:
                    try:
                        self.squeezy.device.close()
                    except Exception:
                        pass
                    self.squeezy.device = None
            # Always confirm pause to LMS (squeezelite sends STMp regardless)
            self.squeezy._send_stat("STMp")
        elif subcommand == "u":
            # Unpause with optional sync timestamp (for multi-room sync)
            target_jiffies = 0
            if len(msg) >= 22:
                target_jiffies = struct.unpack_from(">I", msg, 18)[0]
            self.squeezy.start_at_jiffies = target_jiffies
            import slimproto
            log.debug("Unpause at: %d now: %d", target_jiffies, slimproto.gettime_ms())
            if self.squeezy.paused:
                self.squeezy.paused = False
                self.squeezy._resume_audio()
            elif not self.squeezy.playing and self.squeezy.pcm_buf.available() > 0:
                # Sync mode: sent STMl but LMS hadn't told us to start yet
                self.squeezy._start_audio()
            self.squeezy._send_stat("STMr")
        elif subcommand == "q":
            # Quit — stop playback
            log.debug("Quit command: stopping playback and stream")
            self.squeezy._stop_playback()
            self.squeezy._send_stat("STMf")
        elif subcommand == "f":
            # Flush — graceful stop
            was_active = self.squeezy.streaming or self.squeezy.playing
            log.debug("Flush command: stopping current playback (was_active=%s)", was_active)
            self.squeezy._stop_playback()
            if was_active:
                self.squeezy._send_stat("STMf")
        elif subcommand == "a":
            # Skip ahead — replay_gain field = milliseconds to skip
            if len(msg) >= 22:
                import squeezy as sq_module
                skip_ms = struct.unpack_from(">I", msg, 18)[0]
                skip_frames = int(skip_ms * self.squeezy.current_sample_rate / 1000)
                skip_bytes = skip_frames * sq_module.BYTES_PER_FRAME
                actual = self.squeezy.pcm_buf.skip(skip_bytes)
                skipped_frames = actual // sq_module.BYTES_PER_FRAME
                self.squeezy.output_frames += skipped_frames
                log.debug("Skip ahead: %d ms (%d frames requested, %d skipped)",
                         skip_ms, skip_frames, skipped_frames)
            self.squeezy._send_stat("STMc")

    def handle_strm_start(self, msg):
        """Handle STRM 's' (stream start) message.

        CRITICAL: payload = msg[4:] removes the 4-byte opcode ("strm").
        All offsets below are relative to this stripped payload, NOT the original message.
        When converting message offsets to payload offsets: payload_offset = msg_offset - 4
        (e.g., msg[18] → payload[14], msg[22] → payload[18])
        """
        if len(msg) < 5:
            return

        # Parse strm-s packet structure (lines 5+ are payload)
        payload = msg[4:]
        if len(payload) < 24:
            return

        autostart = payload[1] - ord("0") if len(payload) > 1 else 0
        fmt = chr(payload[2]) if len(payload) > 2 else "?"
        pcm_sample_size = payload[3] if len(payload) > 3 else ord("1")
        pcm_sample_rate = payload[4] if len(payload) > 4 else ord("3")
        pcm_channels = payload[5] if len(payload) > 5 else ord("2")
        pcm_endian = payload[6] if len(payload) > 6 else ord("0")
        threshold = payload[7] * 1024 if len(payload) > 7 else 0

        # Extract replay_gain (16.16 fixed-point at offset 14)
        # Raw value 0 means "no replay gain" → use unity (1.0)
        if len(payload) >= 18:
            replay_gain_raw = struct.unpack_from(">I", payload, 14)[0]
            self.squeezy.replay_gain = replay_gain_raw / 0x10000 if replay_gain_raw else 1.0
        else:
            self.squeezy.replay_gain = 1.0

        # Extract transition parameters (offsets 9-10 in payload)
        if len(payload) >= 11:
            transition_period_raw = payload[9]
            transition_type_raw = payload[10]
            self.squeezy.transition_period_sec = transition_period_raw - ord("0") if transition_period_raw >= ord("0") else 0
            self.squeezy.transition_type = transition_type_raw - ord("0") if transition_type_raw >= ord("0") else 0
        else:
            self.squeezy.transition_type = 0
            self.squeezy.transition_period_sec = 0

        log.debug("Transition: type=%d period=%ds", self.squeezy.transition_type, self.squeezy.transition_period_sec)

        server_port = struct.unpack_from(">H", payload, 18)[0] if len(payload) >= 20 else 0
        server_ip_raw = struct.unpack_from(">I", payload, 20)[0] if len(payload) >= 24 else 0
        http_header = payload[24:] if len(payload) > 24 else b""

        # server_ip == 0 means "same host as the LMS slimproto connection"
        if server_ip_raw == 0:
            server_ip = self.squeezy.server_ip
        else:
            server_ip = socket.inet_ntoa(struct.pack(">I", server_ip_raw))

        # PCM format fields use ASCII digit encoding
        pcm_info = None
        if fmt == "p":
            size_map = {ord("0"): 8, ord("1"): 16, ord("2"): 20, ord("3"): 24, ord("4"): 32}
            bits = size_map.get(pcm_sample_size, 16)
            rate_table = [11025, 22050, 32000, 44100, 48000, 8000, 12000,
                          16000, 24000, 96000, 88200, 176400, 192000, 352800, 384000]
            rate_idx = pcm_sample_rate - ord("0") if pcm_sample_rate >= ord("0") else 0
            rate = rate_table[rate_idx] if rate_idx < len(rate_table) else 44100
            chans = pcm_channels - ord("0") if pcm_channels >= ord("0") else 2
            if chans not in (1, 2):
                chans = 2
            endian = "le" if pcm_endian == ord("1") else "be"
            pcm_info = {"bits": bits, "rate": rate, "channels": chans, "endian": endian}

        # Detect sample rate for this stream
        if fmt == "p" and pcm_info:
            self.squeezy.next_sample_rate = self.squeezy._get_supported_rate(pcm_info["rate"])
        else:
            self.squeezy.next_sample_rate = 44100

        log.debug("Stream start: format=%s server=%s:%d threshold=%d autostart=%d replay_gain=%.2f pcm_info=%s",
                  fmt, server_ip, server_port, threshold, autostart, self.squeezy.replay_gain, pcm_info)

        stream_args = (server_ip, server_port, http_header, threshold, autostart, fmt, pcm_info)

        # Like squeezelite: if audio is still playing, queue next track for gapless
        if self.squeezy.playing and self.squeezy.decode_complete:
            log.debug("Track still playing — queuing next track for gapless transition")
            self.squeezy._pending_track = stream_args
            self.squeezy._send_stat("STMf")
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
        # Start new stream
        self.squeezy._start_stream(*stream_args)

    def handle_audg(self, msg):
        """Handle AUDG message — volume control."""
        if len(msg) < 22:
            return

        adjust = msg[12]
        gain_l = struct.unpack_from(">I", msg, 14)[0]
        gain_r = struct.unpack_from(">I", msg, 18)[0] if len(msg) >= 22 else gain_l

        # Only apply if adjust flag is set
        if adjust:
            self.squeezy.volume = min(gain_l / 0x10000, 1.0)
            log.info("Volume: %.1f%%", self.squeezy.volume * 100)

    def handle_setd(self, msg):
        """Handle SETD message — set player data.

        Matches squeezelite behaviour: respond to id=0 (player name) queries
        and sets with null-terminated name. Ignore unknown setd_ids silently.
        """
        if len(msg) < 5:
            return

        setd_id = msg[4]
        log.debug("SETD: id=%d msg_len=%d", setd_id, len(msg))

        if setd_id == 0:  # Player name
            if len(msg) == 5:
                # Query — return current name (null-terminated, matches squeezelite)
                log.debug("SETD query for player name, responding: %s", self.squeezy.name)
            elif len(msg) > 5:
                # Set — update player name
                new_name = msg[5:].rstrip(b"\x00").decode("utf-8", errors="replace")
                if new_name:
                    self.squeezy.name = new_name
                    try:
                        import config
                        config.save_player_name(new_name)
                    except Exception as e:
                        log.warning("Failed to save player name: %s", e)
                    log.info("Player name set to: %s", self.squeezy.name)
            # Always respond with current name (matches squeezelite)
            name_data = self.squeezy.name.encode("utf-8") + b"\x00"
            import slimproto
            self.squeezy._send(slimproto.build_setd(0, name_data))

    def handle_aude(self, msg):
        """Handle AUDE message (audio end/codec end) — no-op."""
        pass

    def handle_cont(self, msg):
        """Handle CONT message — sync continuation signal."""
        if len(msg) < 5:
            return

        # For sync mode (autostart >= 2), CONT triggers buffer checking
        if self.squeezy.autostart >= 2:
            self.squeezy.autostart -= 2  # Decrement from 2→0 or 3→1
            self.squeezy.cont_received = True
            log.debug("CONT received: autostart now %d", self.squeezy.autostart)

        # Extract metaint if present (for ICY metadata sync)
        if len(msg) >= 8:
            metaint = struct.unpack_from(">I", msg, 4)[0]
            if metaint > 0:
                self.squeezy.icy_meta_int = metaint
                log.debug("CONT metaint: %d", metaint)

    def handle_serv(self, msg):
        """Handle SERV message — server redirect."""
        if len(msg) < 8:
            return

        new_server_ip_raw = struct.unpack_from(">I", msg, 4)[0]
        if new_server_ip_raw:
            self.squeezy.server_ip = socket.inet_ntoa(struct.pack(">I", new_server_ip_raw))
            log.info("Server redirect to %s", self.squeezy.server_ip)

#!/usr/bin/env python3
"""Squeezy - Minimal Squeezebox player for Lyrion Music Server."""

import argparse
import logging
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid

import miniaudio

log = logging.getLogger("squeezy")

SLIMPROTO_PORT = 3483
DEVICE_ID = 12  # squeezeplay device type
VERSION = "0.2.0"
STREAM_BUF_MAX = 2 * 1024 * 1024
SAMPLE_RATE = 44100
CHANNELS = 2
BYTES_PER_FRAME = 4  # 16-bit stereo = 4 bytes per frame


def gettime_ms():
    return int(time.time() * 1000) & 0xFFFFFFFF


def mac_from_string(mac_str):
    return bytes(int(b, 16) for b in mac_str.split(":"))


def default_mac():
    node = uuid.getnode()
    return node.to_bytes(6, "big")


# --- Packet builders ---

def build_helo(mac, caps, reconnect=False, bytes_received=0):
    caps_bytes = caps.encode("ascii")
    payload = struct.pack(
        ">BB6s16sHII2s",
        DEVICE_ID,           # deviceid
        0,                   # revision
        mac,                 # mac
        b"\x00" * 16,        # uuid
        0x4000 if reconnect else 0x0000,  # wlan_channellist
        (bytes_received >> 32) & 0xFFFFFFFF,  # bytes_received_H
        bytes_received & 0xFFFFFFFF,          # bytes_received_L
        b"\x00\x00",         # lang
    ) + caps_bytes
    header = struct.pack(">4sI", b"HELO", len(payload))
    return header + payload


def build_stat(event, stream_buf_size=0, stream_buf_full=0,
               bytes_received=0, output_buf_size=0, output_buf_full=0,
               elapsed_ms=0, server_timestamp=0):
    payload = struct.pack(
        ">4sBBBIIIIHIIIIHIIH",
        event.encode("ascii"),    # event code (4 bytes)
        0,                        # num_crlf
        0,                        # mas_initialized
        0,                        # mas_mode
        stream_buf_size,          # stream_buffer_size
        stream_buf_full,          # stream_buffer_fullness
        (bytes_received >> 32) & 0xFFFFFFFF,  # bytes_received_H
        bytes_received & 0xFFFFFFFF,          # bytes_received_L
        0xFFFF,                   # signal_strength (wired)
        gettime_ms(),             # jiffies
        output_buf_size,          # output_buffer_size
        output_buf_full,          # output_buffer_fullness
        elapsed_ms // 1000,       # elapsed_seconds (u32)
        0,                        # voltage (u16)
        elapsed_ms,               # elapsed_milliseconds (u32)
        server_timestamp,         # server_timestamp (u32)
        0,                        # error_code (u16)
    )
    header = struct.pack(">4sI", b"STAT", len(payload))
    return header + payload


def build_dsco(reason=0):
    payload = struct.pack(">B", reason)
    header = struct.pack(">4sI", b"DSCO", len(payload))
    return header + payload


def build_resp(http_headers):
    header = struct.pack(">4sI", b"RESP", len(http_headers))
    return header + http_headers


def build_setd(player_id, data):
    payload = struct.pack(">B", player_id) + data
    header = struct.pack(">4sI", b"SETD", len(payload))
    return header + payload


# --- PCM Buffer ---

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


# --- Squeezy Player ---

class Squeezy:
    def __init__(self, name="Squeezy", server=None, mac=None, device_id=None):
        self.name = name
        self.server_ip = server
        self.mac = mac_from_string(mac) if mac else default_mac()
        self.audio_device_id = device_id
        self.sock = None
        self.running = False
        self.reconnect = False
        self.bytes_received = 0
        self.server_timestamp = 0

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

        # STAT flags (match squeezelite: only send each once per track)
        self.sent_STMd = False
        self.sent_STMu = False
        self.sent_STMo = False
        self.sent_STMl = False

        self._send_lock = threading.Lock()

    def _capabilities(self):
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
        log.debug("STAT %s elapsed=%dms output_frames=%d stream_bytes=%d",
                  event, elapsed, self.output_frames, self.stream_bytes)
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

    def _elapsed_ms(self):
        # Frame-based elapsed time — accurate regardless of buffering or pauses
        return int(self.output_frames * 1000 / SAMPLE_RATE)

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
                log.info("Retrying in 5 seconds...")
                time.sleep(5)
                continue
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
        buf = bytearray()
        expect_len = None
        timeouts = 0
        last_status = 0

        while self.running:
            # Periodic status while playing
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
                buf.extend(data)
            except socket.timeout:
                timeouts += 1
                if timeouts > 35:
                    log.info("Server timeout")
                    return
                continue

            # Parse messages from buffer
            while True:
                if expect_len is None:
                    if len(buf) < 2:
                        break
                    expect_len = struct.unpack(">H", buf[:2])[0]
                    buf = buf[2:]

                if len(buf) < expect_len:
                    break

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
        log.info("strm command: %s", command)

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
            # Unpause with optional sync timestamp
            target_jiffies = 0
            if len(msg) >= 22:
                target_jiffies = struct.unpack_from(">I", msg, 18)[0]

            self.start_at_jiffies = target_jiffies
            if self.paused:
                self.paused = False
                self._resume_audio()
            elif not self.playing and self.pcm_buf.available() > 0:
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
                log.info("Skip ahead: %d ms (%d frames requested, %d skipped)",
                         skip_ms, skip_frames, skipped_frames)
            self._send_stat("STMc")

        elif command == "q":
            # Hard stop — always send STMf
            self._stop_playback()
            self._send_stat("STMf")

        elif command == "f":
            # Graceful flush — only send STMf if something was active
            was_active = self.streaming or self.playing
            self._stop_playback()
            if was_active:
                self._send_stat("STMf")

    def _handle_strm_start(self, msg):
        if len(msg) < 28:
            log.warning("strm 's' packet too short")
            return

        self.autostart = msg[5] - ord("0") if msg[5] >= ord("0") else 0
        fmt = chr(msg[6])
        pcm_sample_size = msg[7]  # '0'=8, '1'=16, '2'=20, '3'=24, '4'=32
        pcm_sample_rate = msg[8]  # rate index
        pcm_channels = msg[9]     # '1'=mono, '2'=stereo
        pcm_endian = msg[10]      # '0'=big, '1'=little
        threshold = msg[11] * 1024
        server_port = struct.unpack_from(">H", msg, 22)[0]
        server_ip_raw = struct.unpack_from(">I", msg, 24)[0]
        http_header = msg[28:]

        # If server_ip is 0, use the slimproto server
        if server_ip_raw == 0:
            server_ip = self.server_ip
        else:
            server_ip = socket.inet_ntoa(struct.pack(">I", server_ip_raw))

        # Parse PCM format details for ffmpeg input
        # Values are ASCII-encoded per squeezelite protocol (pcm.c)
        pcm_info = None
        if fmt == "p":
            # sample_size: '0'=8bit, '1'=16bit, '2'=20bit, '3'=24bit, '4'=32bit
            size_map = {ord("0"): 8, ord("1"): 16, ord("2"): 20, ord("3"): 24, ord("4"): 32}
            bits = size_map.get(pcm_sample_size, 16)
            # sample_rate: index into squeezelite rate table (ASCII '0'-based)
            rate_table = [11025, 22050, 32000, 44100, 48000, 8000, 12000,
                          16000, 24000, 96000, 88200, 176400, 192000, 352800, 384000]
            rate_idx = pcm_sample_rate - ord("0") if pcm_sample_rate >= ord("0") else 0
            rate = rate_table[rate_idx] if rate_idx < len(rate_table) else 44100
            # channels: ASCII digit
            chans = pcm_channels - ord("0") if pcm_channels >= ord("0") else 2
            if chans not in (1, 2):
                chans = 2
            # endianness: '0'=big, '1'=little
            endian = "le" if pcm_endian == ord("1") else "be"
            pcm_info = {"bits": bits, "rate": rate, "channels": chans, "endian": endian}

        log.info("Stream start: format=%s server=%s:%d threshold=%d autostart=%d pcm=%s",
                 fmt, server_ip, server_port, threshold, self.autostart, pcm_info)

        # Stop any existing playback
        self._stop_playback()
        self._send_stat("STMf")

        # Start streaming in background
        self.streaming = True
        self.stream_bytes = 0
        self.cont_received = (self.autostart < 2)  # autostart < 2 doesn't need cont
        self.pcm_buf.flush()

        self.stream_thread = threading.Thread(
            target=self._stream_worker,
            args=(server_ip, server_port, http_header, threshold, self.autostart, fmt, pcm_info),
            daemon=True,
        )
        self.stream_thread.start()

    def _handle_audg(self, msg):
        # Volume control - log for now
        if len(msg) >= 22:
            adjust = msg[12]
            gain_l = struct.unpack_from(">I", msg, 14)[0]
            gain_r = struct.unpack_from(">I", msg, 18)[0]
            vol = gain_l / 0x10000 if adjust else 1.0
            log.debug("Volume: L=%.2f R=%.2f adjust=%d", gain_l / 0x10000, gain_r / 0x10000, adjust)

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
        log.info("cont received (autostart was %d)", self.autostart)
        if self.autostart >= 2:
            self.autostart -= 2
            self.cont_received = True

    def _handle_serv(self, msg):
        if len(msg) >= 8:
            new_ip = struct.unpack_from(">I", msg, 4)[0]
            if new_ip:
                self.server_ip = socket.inet_ntoa(struct.pack(">I", new_ip))
                log.info("Server redirect to %s", self.server_ip)

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
        log.info("Connecting to stream %s:%d", server_ip, server_port)
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

        log.info("Stream response headers:\n%s", resp_headers.decode("ascii", errors="replace"))

        # Send RESP and STMc
        self._send(build_resp(resp_headers))
        self._send_stat("STMc")

        # For raw PCM at our native format, skip ffmpeg entirely
        pcm_passthrough = (fmt == "p" and pcm_info
                           and pcm_info["bits"] == 16 and pcm_info["endian"] == "le"
                           and pcm_info["rate"] == SAMPLE_RATE
                           and pcm_info["channels"] == CHANNELS)

        if pcm_passthrough:
            log.info("PCM passthrough (no ffmpeg needed)")
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

            # Start decode reader thread
            self.decode_thread = threading.Thread(
                target=self._decode_reader,
                args=(threshold, autostart),
                daemon=True,
            )
            self.decode_thread.start()

            # Feed data to ffmpeg
            self._stream_to_ffmpeg(leftover)

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

                if (not started and autostart >= 1 and self.cont_received
                        and self.pcm_buf.available() >= max(threshold, 8192)):
                    started = True
                    self._start_audio()
                    self._send_stat("STMs")

            except socket.timeout:
                continue
            except OSError:
                break

        # If we never started but have data, start now
        avail = self.pcm_buf.available()
        if not started and avail > 0 and autostart >= 1 and self.cont_received:
            started = True
            self._start_audio()
            self._send_stat("STMs")

        self.decode_complete = True
        log.debug("PCM stream complete, %d bytes buffered (started=%s)", avail, started)

        try:
            self.stream_sock.close()
        except Exception:
            pass
        self.stream_sock = None

    def _stream_to_ffmpeg(self, leftover):
        """Feed HTTP stream data to ffmpeg stdin."""
        if leftover:
            self.stream_bytes += len(leftover)
            try:
                self.ffmpeg_proc.stdin.write(leftover)
            except BrokenPipeError:
                return

        self.stream_sock.settimeout(5)
        while self.streaming and self.running:
            try:
                data = self.stream_sock.recv(32768)
                if not data:
                    break
                self.stream_bytes += len(data)
                try:
                    self.ffmpeg_proc.stdin.write(data)
                except BrokenPipeError:
                    break
            except socket.timeout:
                continue
            except OSError:
                break

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

                # Auto-start playback when threshold reached and cont received (if needed)
                if (not started and autostart >= 1 and self.cont_received
                        and self.pcm_buf.available() >= max(threshold, 8192)):
                    started = True
                    self._start_audio()
                    self._send_stat("STMs")

            except Exception as e:
                log.debug("Decode reader exception: %s", e)
                break

        # If we never started playback but have data, start now
        if not started and self.pcm_buf.available() > 0 and autostart >= 1 and self.cont_received:
            started = True
            self._start_audio()
            self._send_stat("STMs")

        # Mark decode complete - STMd is sent from the audio generator
        # only when actively playing (not while paused)
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
            if self.start_at_jiffies:
                now = gettime_ms()
                diff = (self.start_at_jiffies - now) & 0xFFFFFFFF
                if diff < 0x7FFFFFFF and diff > 0 and diff < 10000:
                    required_frames = yield b"\x00" * required_bytes
                    continue
                # Target reached or passed — clear and start playing
                self.start_at_jiffies = 0

            avail = self.pcm_buf.available()
            if avail > 0:
                n = min(avail, required_bytes)
                chunk = self.pcm_buf.read(n)
                if chunk:
                    self.output_frames += len(chunk) // BYTES_PER_FRAME
                    # Send STMd once when decode completes (while actively playing)
                    if self.decode_complete and not self.sent_STMd:
                        self.sent_STMd = True
                        self._send_stat("STMd")
                    if len(chunk) < required_bytes:
                        chunk += b"\x00" * (required_bytes - len(chunk))
                    required_frames = yield chunk
                    continue

            # Buffer empty
            if self.decode_complete and avail == 0:
                # Track fully played — send STMu once
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

    def _start_audio(self):
        if self.playing:
            return
        log.info("Starting audio playback")
        try:
            self.device = miniaudio.PlaybackDevice(
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=CHANNELS,
                sample_rate=SAMPLE_RATE,
                device_id=self.audio_device_id,
            )
            gen = self._audio_generator()
            next(gen)  # prime the generator before miniaudio calls send()
            self.device.start(gen)
            self.playing = True
            self.paused = False
            self.output_frames = 0
        except Exception as e:
            log.error("Audio start failed: %s", e)

    def _start_audio_at_time(self):
        """Start audio device immediately but output silence until sync timestamp.
        The generator handles the silence-until-time logic (OUTPUT_START_AT equivalent)."""
        log.info("Sync start at jiffies=%d (now=%d)", self.start_at_jiffies, gettime_ms())
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


def main():
    parser = argparse.ArgumentParser(description="Squeezy - Minimal Squeezebox player")
    parser.add_argument("-s", "--server", help="LMS server IP (auto-discover if not set)")
    parser.add_argument("-n", "--name", default="Squeezy", help="Player name (default: Squeezy)")
    parser.add_argument("-m", "--mac", help="MAC address aa:bb:cc:dd:ee:ff (auto-detect if not set)")
    parser.add_argument("-d", "--device", help="Audio output device (name or substring, e.g. 'HDMI')")
    parser.add_argument("-l", "--list-devices", action="store_true", help="List audio output devices and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

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

    player = Squeezy(name=args.name, server=args.server, mac=args.mac, device_id=device_id)

    def handle_signal(sig, frame):
        player.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    player.run()


if __name__ == "__main__":
    main()

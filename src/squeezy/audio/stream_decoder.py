#!/usr/bin/env python3
"""Stream decoding pipeline: HTTP fetch, FFmpeg decode, ICY metadata, PCM buffering.

Handles audio stream downloading from LMS, optional FFmpeg transcoding,
in-stream ICY metadata extraction, and buffering for the audio generator.
"""

import errno
import logging
import socket
import ssl
import subprocess
import threading
import time

log = logging.getLogger("squeezy")


class PCMBuffer:
    """Thread-safe circular PCM audio buffer.

    Three threads share this buffer:
    - Writer (stream thread): downloads and decodes audio, calls .write()
    - Reader (miniaudio callback): audio generator, calls .read()
    - Control (main thread): calls .flush() on stop/skip
    """

    def __init__(self):
        """Initialize empty buffer."""
        self.buf = bytearray()
        self.lock = threading.Lock()

    def write(self, data):
        """Append data to buffer (thread-safe).

        Args:
            data: Bytes to append
        """
        with self.lock:
            self.buf.extend(data)

    def read(self, n):
        """Read and remove n bytes from buffer (thread-safe).

        Args:
            n: Number of bytes to read

        Returns:
            Bytes read (may be less than n if buffer has less)
        """
        with self.lock:
            chunk = bytes(self.buf[:n])
            del self.buf[:n]
            return chunk

    def available(self):
        """Get number of bytes currently in buffer (thread-safe).

        Returns:
            Bytes available
        """
        with self.lock:
            return len(self.buf)

    def skip(self, n):
        """Skip n bytes without returning them.

        Args:
            n: Number of bytes to skip

        Returns:
            Bytes actually skipped
        """
        with self.lock:
            skipped = min(n, len(self.buf))
            del self.buf[:skipped]
            return skipped

    def flush(self):
        """Clear entire buffer (thread-safe)."""
        with self.lock:
            self.buf.clear()


class StreamDecoder:
    """Handles HTTP stream fetching and optional FFmpeg decoding."""

    def __init__(self, squeezy_ref):
        """Initialize stream decoder.

        Args:
            squeezy_ref: Reference to Squeezy instance
        """
        self.squeezy = squeezy_ref

    def start(self, server_ip, server_port, http_header, threshold, autostart, fmt, pcm_info):
        """Start streaming from LMS.

        Spawns stream_worker thread that handles HTTP connection and optional
        FFmpeg decoding.

        Args:
            server_ip: Stream server IP address
            server_port: Stream server port
            http_header: Raw HTTP request bytes
            threshold: Buffer threshold in bytes before playback starts
            autostart: Autostart mode (0-3) for sync
            fmt: Codec format ('p'=PCM, 'm'=MP3, 'f'=FLAC, etc.)
            pcm_info: PCM format dict (for raw PCM) or None
        """
        self.squeezy.stream_thread = threading.Thread(
            target=self._stream_worker,
            args=(server_ip, server_port, http_header, threshold, autostart, fmt, pcm_info),
            daemon=True,
        )
        self.squeezy.stream_thread.start()

    def stop(self):
        """Stop streaming and clean up."""
        self.squeezy.streaming = False
        if self.squeezy.stream_sock:
            try:
                self.squeezy.stream_sock.close()
            except Exception:
                pass
            self.squeezy.stream_sock = None
        self._cleanup_ffmpeg()
        if self.squeezy.stream_thread and self.squeezy.stream_thread.is_alive():
            self.squeezy.stream_thread.join(timeout=5)
        self.squeezy.stream_thread = None

    def _stream_worker(self, server_ip, server_port, http_header, threshold, autostart, fmt, pcm_info):
        """Stream worker thread — downloads and feeds audio to decoder or buffer.

        This runs in a separate thread and handles the entire HTTP stream lifecycle.
        """
        try:
            self._do_stream(server_ip, server_port, http_header, threshold, autostart, fmt, pcm_info)
        except Exception as e:
            log.warning("Stream error: %s", e)
        finally:
            # Wait for decode reader to finish (if exists)
            if self.squeezy.decode_thread and self.squeezy.decode_thread.is_alive():
                self.squeezy.decode_thread.join(timeout=10)
            self.squeezy.streaming = False
            self._cleanup_ffmpeg()
            try:
                from ..protocol import slimproto
                self.squeezy._send(slimproto.build_dsco(0))
            except Exception:
                pass

    def _do_stream(self, server_ip, server_port, http_header, threshold, autostart, fmt, pcm_info):
        """Connect to stream server and handle HTTP response."""
        # Connect to stream server
        log.debug("Connecting to stream %s:%d", server_ip, server_port)
        self.squeezy.stream_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.squeezy.stream_sock.settimeout(10)
        self.squeezy.stream_sock.connect((server_ip, server_port))

        # Wrap with SSL if port is 443 (HTTPS)
        if server_port == 443:
            try:
                context = ssl.create_default_context()
                self.squeezy.stream_sock = context.wrap_socket(
                    self.squeezy.stream_sock,
                    server_hostname=server_ip
                )
                log.debug("SSL/TLS negotiated for HTTPS stream")
            except Exception as e:
                log.warning("SSL negotiation failed, continuing with HTTP: %s", e)

        # Send HTTP request
        self.squeezy.stream_sock.sendall(http_header)

        # Read HTTP response headers
        resp_buf = bytearray()
        while b"\r\n\r\n" not in resp_buf:
            chunk = self.squeezy.stream_sock.recv(4096)
            if not chunk:
                log.warning("Stream closed during headers")
                return
            resp_buf.extend(chunk)

        header_end = resp_buf.index(b"\r\n\r\n") + 4
        resp_headers = bytes(resp_buf[:header_end])
        leftover = bytes(resp_buf[header_end:])

        log.debug("Stream response headers:\n%s", resp_headers.decode("ascii", errors="replace"))

        # Parse ICY metadata interval (for in-stream metadata like Shoutcast)
        self.squeezy.icy_meta_int = 0
        try:
            headers_str = resp_headers.decode("ascii", errors="replace")
            for line in headers_str.split("\r\n"):
                if line.lower().startswith("icy-metaint:"):
                    self.squeezy.icy_meta_int = int(line.split(":", 1)[1].strip())
                    log.debug("ICY metadata interval: %d bytes", self.squeezy.icy_meta_int)
                    break
        except Exception:
            pass

        # Send RESP and STMc packets to LMS
        from ..protocol import slimproto
        self.squeezy._send(slimproto.build_resp(resp_headers))
        self.squeezy._send_stat("STMc")

        # For raw PCM at native format, skip ffmpeg entirely
        from .. import squeezy as sq_module
        pcm_passthrough = (fmt == "p" and pcm_info
                           and pcm_info["bits"] == 16 and pcm_info["endian"] == "le"
                           and pcm_info["rate"] == sq_module.SAMPLE_RATE
                           and pcm_info["channels"] == sq_module.CHANNELS)

        if pcm_passthrough:
            log.debug("PCM passthrough (no ffmpeg needed)")
            # Feed HTTP body directly to PCM buffer
            self._stream_to_buffer(leftover, threshold, autostart)
        else:
            # Build ffmpeg command — specify input format for raw PCM
            ffmpeg_cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
            if fmt == "p" and pcm_info:
                ffmpeg_cmd.extend([
                    "-f", "s{0}{1}".format(pcm_info["bits"], pcm_info["endian"]),
                    "-ar", str(pcm_info["rate"]),
                    "-ac", str(pcm_info["channels"]),
                ])
            # Input from stdin, output: always s16le PCM at target sample rate
            ffmpeg_cmd.extend(["-i", "pipe:0",
                               "-f", "s16le", "-ar", str(self.squeezy.next_sample_rate),
                               "-ac", str(sq_module.CHANNELS),
                               "pipe:1"])

            log.debug("FFmpeg command: %s", " ".join(ffmpeg_cmd))
            self.squeezy.ffmpeg_proc = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.squeezy.decode_thread = threading.Thread(
                target=self._decode_reader,
                args=(threshold, autostart),
                daemon=True,
            )
            self.squeezy.decode_thread.start()
            # Feed HTTP body to ffmpeg stdin
            self._stream_to_ffmpeg(leftover, threshold, autostart)

    def _stream_to_ffmpeg(self, leftover, threshold, autostart):
        """Stream HTTP body to ffmpeg stdin with ICY metadata handling."""
        try:
            bytes_sent = 0
            bytes_since_meta = 0

            # Seed with leftover from HTTP headers
            if leftover:
                self.squeezy.ffmpeg_proc.stdin.write(leftover)
                bytes_sent += len(leftover)
                bytes_since_meta += len(leftover)
                log.debug("FFmpeg stdin: seeded with %d leftover bytes", len(leftover))

            self.squeezy.stream_sock.settimeout(5)
            while self.squeezy.streaming:
                try:
                    chunk = self.squeezy.stream_sock.recv(32768)
                    if not chunk:
                        log.debug("Stream closed after %d bytes", bytes_sent)
                        break

                    # Handle ICY metadata if metaint is set
                    if self.squeezy.icy_meta_int > 0:
                        # Check if we've hit a metadata boundary
                        while bytes_since_meta + len(chunk) > self.squeezy.icy_meta_int:
                            # Write up to metadata boundary
                            to_write = self.squeezy.icy_meta_int - bytes_since_meta
                            self.squeezy.ffmpeg_proc.stdin.write(chunk[:to_write])
                            bytes_sent += to_write
                            chunk = chunk[to_write:]

                            # Read and parse metadata block
                            meta_len_byte = self.squeezy.stream_sock.recv(1)
                            if not meta_len_byte:
                                break
                            meta_len = meta_len_byte[0] * 16
                            if meta_len > 0:
                                meta_data = self.squeezy.stream_sock.recv(meta_len)
                                if meta_data:
                                    from ..config import metadata
                                    result = metadata.parse_icy_metadata(meta_len_byte + meta_data)
                                    if result["title"]:
                                        log.info("Track: %s (from ICY metadata)", result["title"])
                                        self.squeezy.icy_title = result["title"]
                                        self.squeezy.icy_artist = result.get("artist", "")
                                        self.squeezy.icy_album = result.get("album", "")
                            bytes_since_meta = 0
                        else:
                            # No metadata boundary in this chunk
                            self.squeezy.ffmpeg_proc.stdin.write(chunk)
                            bytes_sent += len(chunk)
                            bytes_since_meta += len(chunk)
                    else:
                        # No metadata
                        self.squeezy.ffmpeg_proc.stdin.write(chunk)
                        bytes_sent += len(chunk)

                except socket.timeout:
                    continue
                except BrokenPipeError:
                    log.debug("FFmpeg stdin broken pipe after %d bytes", bytes_sent)
                    break
                except OSError as e:
                    if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                        continue
                    log.debug("Stream to ffmpeg error: %s after %d bytes", e, bytes_sent)
                    break
        finally:
            log.debug("FFmpeg stdin done: %d bytes written, streaming=%s", bytes_sent, self.squeezy.streaming)
            try:
                self.squeezy.ffmpeg_proc.stdin.close()
            except Exception:
                pass

    def _stream_to_buffer(self, leftover, threshold, autostart):
        """Stream HTTP body directly to PCM buffer (raw PCM passthrough)."""
        started = False
        try:
            bytes_received = 0
            if leftover:
                self.squeezy.pcm_buf.write(leftover)
                bytes_received += len(leftover)
                self.squeezy.stream_bytes = bytes_received

            self.squeezy.stream_sock.settimeout(5)
            while self.squeezy.streaming:
                try:
                    chunk = self.squeezy.stream_sock.recv(32768)
                    if not chunk:
                        log.debug("Stream closed after %d bytes", bytes_received)
                        break
                    self.squeezy.pcm_buf.write(chunk)
                    bytes_received += len(chunk)
                    self.squeezy.stream_bytes = bytes_received

                    if not started and self.squeezy.cont_received:
                        self._check_threshold_start(threshold, autostart)
                        started = self.squeezy.playing or self.squeezy.sent_STMl
                except socket.timeout:
                    continue
                except OSError as e:
                    if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.ECONNRESET, errno.ECONNABORTED):
                        log.debug("Stream recv transient error %d — retrying", e.errno)
                        time.sleep(0.1)
                        continue
                    log.debug("Stream error: %s", e)
                    break
        finally:
            # If we never started but have data, try now (short tracks)
            if not started and self.squeezy.pcm_buf.available() > 0 and self.squeezy.cont_received:
                self._check_threshold_start(threshold, autostart, force=True)

            self.squeezy.decode_complete = True
            avail = self.squeezy.pcm_buf.available()
            log.debug("PCM stream complete, %d bytes buffered (started=%s)", avail, started)

            try:
                self.squeezy.stream_sock.close()
            except Exception:
                pass
            self.squeezy.stream_sock = None

    def _decode_reader(self, threshold, autostart):
        """Read decoded PCM from ffmpeg stdout and feed to buffer."""
        started = False
        try:
            bytes_received = 0
            while self.squeezy.streaming:
                if not self.squeezy.ffmpeg_proc:
                    log.debug("Decode reader: ffmpeg_proc is None, exiting")
                    break
                chunk = self.squeezy.ffmpeg_proc.stdout.read(8192)
                if not chunk:
                    log.debug("FFmpeg closed after %d bytes", bytes_received)
                    break
                self.squeezy.pcm_buf.write(chunk)
                bytes_received += len(chunk)
                self.squeezy.stream_bytes = bytes_received

                if not started and self.squeezy.cont_received:
                    self._check_threshold_start(threshold, autostart)
                    started = self.squeezy.playing or self.squeezy.sent_STMl
        except Exception as e:
            log.warning("Decode reader error: %s", e)
        finally:
            # If we never started but have data, try now (short tracks)
            if not started and self.squeezy.pcm_buf.available() > 0 and self.squeezy.cont_received:
                self._check_threshold_start(threshold, autostart, force=True)

            # Check ffmpeg exit code — send error packet if non-zero
            if self.squeezy.ffmpeg_proc:
                exit_code = self.squeezy.ffmpeg_proc.returncode
                if exit_code and exit_code != 0:
                    log.warning("ffmpeg exited with code %d — sending error packet", exit_code)
                    self.squeezy._send_stat("STMn")
                else:
                    self.squeezy.decode_complete = True
            else:
                self.squeezy.decode_complete = True

    def _cleanup_ffmpeg(self):
        """Clean up ffmpeg subprocess."""
        if not self.squeezy.ffmpeg_proc:
            return
        try:
            self.squeezy.ffmpeg_proc.stdin.close()
        except Exception:
            pass
        try:
            self.squeezy.ffmpeg_proc.kill()
            self.squeezy.ffmpeg_proc.wait(timeout=2)
        except Exception:
            pass
        self.squeezy.ffmpeg_proc = None

    def _check_threshold_start(self, threshold, autostart, force=False):
        """Check if buffer has reached threshold and start playback if needed.

        Uses self.squeezy.autostart (live value, updated by CONT handler) rather
        than the hint passed in, since CONT changes it from 2→0.
        """
        if self.squeezy.playing or self.squeezy.sent_STMl:
            return  # Already started or signalled

        avail = self.squeezy.pcm_buf.available()
        if not force and avail < max(threshold, 8192):
            return  # Threshold not yet reached

        # Use the live autostart value (CONT may have decremented it)
        live_autostart = self.squeezy.autostart

        if live_autostart >= 1:
            # Normal mode: start audio immediately
            log.debug("Buffer threshold reached (%d bytes) — starting audio", avail)
            self.squeezy._start_audio()
            self.squeezy._send_stat("STMs")
        elif live_autostart == 0 and not self.squeezy.sent_STMl:
            # Sync mode: signal readiness to LMS, don't start audio yet
            self.squeezy.sent_STMl = True
            log.info("Buffer threshold reached — signalling ready (STMl) for sync")
            self.squeezy._send_stat("STMl")

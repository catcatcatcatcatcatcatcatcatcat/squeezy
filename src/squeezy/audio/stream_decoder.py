#!/usr/bin/env python3
"""Thread-safe PCM audio buffer for the streaming pipeline.

The PCMBuffer sits between the audio source (HTTP stream or FFmpeg decoder)
and the audio sink (miniaudio playback device). Three threads share it:

    stream/decode thread  →  .write()  →  PCMBuffer  →  .read()  →  miniaudio callback
                                              ↑
                              main thread  →  .flush()  (on stop/skip)

Memory is bounded: writes block when the buffer is full, applying
backpressure to the upstream decoder (ffmpeg). This prevents OOM
while ensuring no audio data is lost.

The actual HTTP streaming, FFmpeg process management, and ICY metadata
parsing are handled directly by the Squeezy class in squeezy.py.
"""

import threading

from ..protocol import slimproto


class PCMBuffer:
    """Thread-safe bounded PCM audio buffer.

    Three threads share this buffer:
    - Writer (stream thread): downloads and decodes audio, calls .write()
    - Reader (miniaudio callback): audio generator, calls .read()
    - Control (main thread): calls .flush() on stop/skip

    Memory is bounded: writes block when the buffer is full, applying
    backpressure to the upstream decoder (ffmpeg). This prevents OOM
    while ensuring no audio data is lost.
    """

    # Default max buffer: ~23 seconds at 44100Hz stereo 16-bit (4 MB).
    # This is the physical buffer limit; STREAM_BUF_MAX (2 MB) is what we
    # report to LMS. The physical buffer is larger to absorb decode bursts.
    MAX_SIZE = slimproto.PCM_BUF_MAX_SIZE

    def __init__(self, max_size=None):
        """Initialize empty buffer.

        Args:
            max_size: Maximum buffer size in bytes (default: 4MB).
                      Set to 0 for unlimited (testing only).
        """
        self.buf = bytearray()
        self.lock = threading.Lock()
        self._not_full = threading.Condition(self.lock)
        self.max_size = max_size if max_size is not None else self.MAX_SIZE
        self._closed = False

    def write(self, data):
        """Append data to buffer, blocking until all data is written.

        Blocks in a loop until the entire chunk is consumed. This applies
        backpressure to ffmpeg via its stdout pipe — ffmpeg blocks on write
        when the pipe is full, which happens when we stop reading because
        our buffer is full.

        Args:
            data: Bytes to append

        Returns:
            Number of bytes actually written (0 if closed before any write)
        """
        if not self.max_size:
            with self._not_full:
                self.buf.extend(data)
                return len(data)
        offset = 0
        total = len(data)
        with self._not_full:
            while offset < total:
                # Wait for space
                while len(self.buf) >= self.max_size and not self._closed:
                    self._not_full.wait(timeout=0.1)
                if self._closed:
                    return offset
                space = self.max_size - len(self.buf)
                end = min(offset + space, total)
                self.buf.extend(data[offset:end])
                offset = end
        return offset

    def read(self, n):
        """Read and remove n bytes from buffer (thread-safe).

        Wakes any blocked writers after freeing space.

        Args:
            n: Number of bytes to read

        Returns:
            Bytes read (may be less than n if buffer has less)
        """
        with self._not_full:
            chunk = bytes(self.buf[:n])
            del self.buf[:n]
            if chunk:
                self._not_full.notify()
            return chunk

    def available(self):
        """Get number of bytes currently in buffer (thread-safe).

        Returns:
            Bytes available for reading
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
        with self._not_full:
            skipped = min(n, len(self.buf))
            del self.buf[:skipped]
            if skipped:
                self._not_full.notify()
            return skipped

    def flush(self):
        """Clear entire buffer and wake blocked writers."""
        with self._not_full:
            self.buf.clear()
            self._closed = False
            self._not_full.notify()

    def close(self):
        """Signal writers to stop blocking (used during shutdown)."""
        with self._not_full:
            self._closed = True
            self._not_full.notify_all()

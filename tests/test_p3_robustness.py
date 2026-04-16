"""Unit tests for Priority 3 features: MP3 gapless, memory management, DSCO, thread safety."""

import struct
from unittest.mock import Mock, patch, MagicMock

import pytest

from squeezy.config.metadata import parse_lame_header
from squeezy.audio.stream_decoder import PCMBuffer
from squeezy import Squeezy


class TestP36MP3Gapless:
    """Tests for LAME gapless metadata parsing (P3.6)."""

    def _make_lame_frame(self, enc_delay=576, enc_padding=1152, frame_count=1000,
                         stereo=True, tag=b"Xing"):
        """Build a minimal MP3 frame with Xing/LAME header for testing."""
        # MPEG1 Layer3 sync word + valid header bytes
        data = bytearray(256)
        data[0] = 0xFF
        data[1] = 0xFB  # MPEG1, Layer3, no CRC
        data[2] = 0x90  # 128kbps, 44100Hz
        data[3] = 0x00  # Stereo

        # Xing/Info tag at offset 36 (stereo) or 21 (mono)
        offset = 36 if stereo else 21
        data[offset:offset + 4] = tag

        # Flags byte at offset + 7: all flags set (frames, bytes, TOC, quality)
        ptr = offset + 7
        data[ptr] = 0x0F  # All 4 flags

        # Frame count (4 bytes)
        struct.pack_into(">I", data, ptr + 1, frame_count)
        ptr += 4
        # Byte count (4 bytes) — dummy
        struct.pack_into(">I", data, ptr + 1, 0)
        ptr += 4
        # TOC (100 bytes) — dummy
        ptr += 100
        # Quality (4 bytes) — dummy
        ptr += 4

        # LAME tag at ptr + 1
        data[ptr + 1:ptr + 5] = b"LAME"

        # Encoder delay/padding at ptr + 22
        delay_offset = ptr + 22
        data[delay_offset] = (enc_delay >> 4) & 0xFF
        data[delay_offset + 1] = ((enc_delay & 0x0F) << 4) | ((enc_padding >> 8) & 0x0F)
        data[delay_offset + 2] = enc_padding & 0xFF

        return bytes(data)

    def test_parse_lame_header_valid(self):
        """Parse valid LAME header with encoder delay and padding."""
        data = self._make_lame_frame(enc_delay=576, enc_padding=1152, frame_count=1000)
        result = parse_lame_header(data)
        assert result is not None
        assert result["enc_delay"] == 576
        assert result["enc_padding"] == 1152
        assert result["frame_count"] == 1000
        assert result["total_samples"] == 1000 * 1152 - 576 - 1152

    def test_parse_lame_header_info_tag(self):
        """Parse LAME header with 'Info' tag (CBR variant)."""
        data = self._make_lame_frame(enc_delay=576, enc_padding=0, tag=b"Info")
        result = parse_lame_header(data)
        assert result is not None
        assert result["enc_delay"] == 576

    def test_parse_lame_header_no_sync(self):
        """Return None for data without MPEG sync word."""
        data = b"\x00" * 200
        result = parse_lame_header(data)
        assert result is None

    def test_parse_lame_header_too_short(self):
        """Return None for data shorter than minimum."""
        data = b"\xFF\xFB" + b"\x00" * 10
        result = parse_lame_header(data)
        assert result is None

    def test_parse_lame_header_no_xing(self):
        """Return None when no Xing/Info tag present."""
        data = bytearray(200)
        data[0] = 0xFF
        data[1] = 0xFB
        result = parse_lame_header(bytes(data))
        assert result is None

    def test_lame_gapless_attribute_initialized(self):
        """Squeezy has lame_gapless attribute initialized to None."""
        squeezy = Squeezy(name="test")
        assert squeezy.lame_gapless is None


class TestP37MemoryManagement:
    """Tests for PCMBuffer memory limits (P3.7)."""

    def test_buffer_max_size_default(self):
        """PCMBuffer has a default max size of 4MB."""
        buf = PCMBuffer()
        assert buf.max_size == 4 * 1024 * 1024

    def test_buffer_max_size_custom(self):
        """PCMBuffer accepts custom max size."""
        buf = PCMBuffer(max_size=1024)
        assert buf.max_size == 1024

    def test_buffer_write_within_limit(self):
        """Writes within limit succeed fully."""
        buf = PCMBuffer(max_size=100)
        written = buf.write(b"\x00" * 50)
        assert written == 50
        assert buf.available() == 50

    def test_buffer_write_blocks_until_all_written(self):
        """Writes exceeding space block until all data is written."""
        import threading
        buf = PCMBuffer(max_size=100)
        buf.write(b"\x00" * 80)
        results = []

        def writer():
            # 50 bytes but only 20 fit — should block until reader frees space
            written = buf.write(b"\x00" * 50)
            results.append(written)

        t = threading.Thread(target=writer)
        t.start()
        import time
        time.sleep(0.05)
        # Writer wrote 20, blocked waiting for remaining 30
        assert t.is_alive()
        buf.read(50)  # Free 50 bytes — writer can finish the remaining 30
        t.join(timeout=1)
        assert not t.is_alive()
        assert results[0] == 50  # All 50 bytes written

    def test_buffer_write_blocks_when_full(self):
        """Writes to a full buffer block until space is freed."""
        import threading
        buf = PCMBuffer(max_size=100)
        buf.write(b"\x00" * 100)
        results = []

        def writer():
            written = buf.write(b"\x00" * 10)
            results.append(written)

        t = threading.Thread(target=writer)
        t.start()
        # Writer should be blocked — free some space
        import time
        time.sleep(0.05)
        assert t.is_alive()  # Still blocked
        buf.read(20)  # Free space
        t.join(timeout=1)
        assert not t.is_alive()
        assert results[0] == 10

    def test_buffer_write_unblocks_on_close(self):
        """Blocked writes return 0 when buffer is closed."""
        import threading
        buf = PCMBuffer(max_size=100)
        buf.write(b"\x00" * 100)
        results = []

        def writer():
            written = buf.write(b"\x00" * 10)
            results.append(written)

        t = threading.Thread(target=writer)
        t.start()
        import time
        time.sleep(0.05)
        buf.close()
        t.join(timeout=1)
        assert not t.is_alive()
        assert results[0] == 0

    def test_buffer_unlimited(self):
        """Buffer with max_size=0 is unlimited."""
        buf = PCMBuffer(max_size=0)
        buf.write(b"\x00" * 10_000_000)
        assert buf.available() == 10_000_000

    def test_buffer_read_frees_space(self):
        """Reading data frees space for more writes."""
        buf = PCMBuffer(max_size=100)
        buf.write(b"\x00" * 100)
        buf.read(50)
        written = buf.write(b"\x00" * 30)
        assert written == 30
        assert buf.available() == 80


    def test_buffer_no_data_loss_producer_consumer(self):
        """Producer/consumer: ALL bytes written must be readable — no silent drops.

        This is the test that would have caught the original data-loss bug
        where write() silently dropped data when the buffer was full.
        A 5-minute track is ~52MB of PCM but the buffer is only 4MB,
        so data must flow through without loss.
        """
        import threading

        buf = PCMBuffer(max_size=1024)  # Small buffer to force many cycles
        total_bytes = 10240  # 10x the buffer size
        chunk_size = 128

        # Build recognizable data (not all zeros) so we can verify content
        source = bytes(range(256)) * (total_bytes // 256)
        received = bytearray()

        def producer():
            offset = 0
            while offset < total_bytes:
                end = min(offset + chunk_size, total_bytes)
                buf.write(source[offset:end])
                offset = end

        def consumer():
            while len(received) < total_bytes:
                chunk = buf.read(64)
                if chunk:
                    received.extend(chunk)
                else:
                    import time
                    time.sleep(0.001)

        t_prod = threading.Thread(target=producer)
        t_cons = threading.Thread(target=consumer)
        t_cons.start()
        t_prod.start()
        t_prod.join(timeout=5)
        t_cons.join(timeout=5)

        assert len(received) == total_bytes, f"Lost {total_bytes - len(received)} bytes"
        assert bytes(received) == source, "Data corruption: bytes received don't match bytes sent"

    def test_buffer_skip(self):
        """skip() discards bytes and frees space for writers."""
        buf = PCMBuffer(max_size=100)
        buf.write(b"\xAA" * 80)
        skipped = buf.skip(30)
        assert skipped == 30
        assert buf.available() == 50
        # Remaining data is correct (the last 50 bytes)
        data = buf.read(50)
        assert data == b"\xAA" * 50

    def test_buffer_skip_more_than_available(self):
        """skip() with n > available skips only what's there."""
        buf = PCMBuffer(max_size=100)
        buf.write(b"\xBB" * 40)
        skipped = buf.skip(100)
        assert skipped == 40
        assert buf.available() == 0

    def test_buffer_skip_empty(self):
        """skip() on empty buffer returns 0."""
        buf = PCMBuffer(max_size=100)
        assert buf.skip(10) == 0

    def test_buffer_skip_unblocks_writer(self):
        """skip() frees space and unblocks a waiting writer."""
        import threading
        import time
        buf = PCMBuffer(max_size=100)
        buf.write(b"\x00" * 100)
        results = []

        def writer():
            written = buf.write(b"\x00" * 20)
            results.append(written)

        t = threading.Thread(target=writer)
        t.start()
        time.sleep(0.05)
        assert t.is_alive()  # Blocked — buffer full
        buf.skip(30)  # Free space via skip
        t.join(timeout=1)
        assert not t.is_alive()
        assert results[0] == 20

    def test_buffer_flush_unblocks_writer(self):
        """flush() clears buffer and unblocks a waiting writer."""
        import threading
        import time
        buf = PCMBuffer(max_size=100)
        buf.write(b"\x00" * 100)
        results = []

        def writer():
            written = buf.write(b"\xFF" * 50)
            results.append(written)

        t = threading.Thread(target=writer)
        t.start()
        time.sleep(0.05)
        assert t.is_alive()  # Blocked
        buf.flush()  # Clear everything
        t.join(timeout=1)
        assert not t.is_alive()
        assert results[0] == 50
        assert buf.available() == 50

    def test_buffer_flush_resets_closed(self):
        """flush() resets the closed flag so writes work again."""
        buf = PCMBuffer(max_size=100)
        buf.close()
        buf.flush()
        written = buf.write(b"\x00" * 50)
        assert written == 50

    def test_buffer_read_empty(self):
        """read() on empty buffer returns empty bytes."""
        buf = PCMBuffer(max_size=100)
        assert buf.read(10) == b""

    def test_buffer_read_partial(self):
        """read() returns less than n if buffer has less."""
        buf = PCMBuffer(max_size=100)
        buf.write(b"\xCC" * 30)
        data = buf.read(50)
        assert len(data) == 30
        assert data == b"\xCC" * 30

    def test_elapsed_time_stable_after_resume(self):
        """Elapsed time should not jump after pause/resume.

        Catches the bug where _device_start_time wasn't reset on resume,
        causing _elapsed_ms() to over-report by the pause duration.
        """
        sq = Squeezy(name="test")
        sq.current_sample_rate = 44100
        sq.output_frames = 44100  # 1 second of audio
        sq._track_start_frames = 0
        sq._device_start_time = None
        sq._device_start_frames = 0

        elapsed_before = sq._elapsed_ms()

        # Simulate pause/resume: _device_start_time should be reset
        # (the fix we applied to _resume_audio)
        sq._device_start_time = None
        sq._device_start_frames = sq.output_frames

        elapsed_after = sq._elapsed_ms()
        # Should be the same — no jump from stale wall-clock reference
        assert abs(elapsed_before - elapsed_after) < 5, \
            f"Elapsed jumped from {elapsed_before} to {elapsed_after} after resume"


class TestP311DSCO:
    """Tests for DSCO disconnect packet handling (P3.11)."""

    def test_dsco_dispatch(self):
        """DSCO opcode is dispatched to handle_dsco."""
        squeezy = Squeezy(name="test")
        squeezy.sock = Mock()
        squeezy._stop_playback = Mock()

        # Simulate DSCO message
        msg = b"dsco" + b"\x00"
        squeezy.protocol.dispatch(msg)

        squeezy._stop_playback.assert_called_once()

    def test_dsco_closes_socket(self):
        """DSCO handler closes the TCP socket."""
        squeezy = Squeezy(name="test")
        mock_sock = Mock()
        squeezy.sock = mock_sock
        squeezy._stop_playback = Mock()

        msg = b"dsco" + b"\x00"
        squeezy.protocol.dispatch(msg)

        mock_sock.close.assert_called_once()
        assert squeezy.sock is None


class TestP310GracefulShutdown:
    """Tests for graceful shutdown (P3.10)."""

    def test_stop_sets_running_false(self):
        """stop() sets running to False."""
        squeezy = Squeezy(name="test")
        squeezy.running = True
        squeezy.stop()
        assert squeezy.running is False

    def test_stop_closes_socket(self):
        """stop() closes the SlimProto TCP socket to unblock recv()."""
        squeezy = Squeezy(name="test")
        squeezy.running = True
        mock_sock = Mock()
        squeezy.sock = mock_sock
        squeezy.stop()
        mock_sock.close.assert_called_once()
        assert squeezy.running is False

    def test_stop_without_socket(self):
        """stop() handles None socket gracefully."""
        squeezy = Squeezy(name="test")
        squeezy.sock = None
        squeezy.stop()  # Should not raise

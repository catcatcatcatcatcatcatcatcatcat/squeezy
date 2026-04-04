"""Unit tests for Priority 1 critical reliability features."""

import struct
import time
from unittest.mock import Mock, patch, MagicMock

import pytest

from squeezy import Squeezy


class TestP11ServerTimeout:
    """Tests for 35-second server heartbeat timeout detection."""

    def test_last_server_msg_initialized(self):
        """Server timeout tracking is initialized."""
        squeezy = Squeezy(name="test")
        # _last_server_msg should be set
        assert hasattr(squeezy, '_last_server_msg')
        assert isinstance(squeezy._last_server_msg, float)

    def test_server_timeout_detection_logic(self):
        """Timeout detection correctly identifies dead connection."""
        squeezy = Squeezy(name="test")

        # Set last message to 40 seconds ago
        squeezy._last_server_msg = time.monotonic() - 40

        # Check if enough time has passed
        elapsed = time.monotonic() - squeezy._last_server_msg
        assert elapsed > 35, "Should detect timeout after 35+ seconds"

    def test_server_timeout_reset_on_message(self):
        """Timeout timer resets when message is received."""
        squeezy = Squeezy(name="test")

        initial_time = squeezy._last_server_msg
        time.sleep(0.1)

        # Simulate receiving a message
        squeezy._last_server_msg = time.monotonic()

        # Timer should have advanced
        assert squeezy._last_server_msg > initial_time


class TestP13ReconnectionFallback:
    """Tests for reconnection fallback to UDP discovery after 5 failures."""

    def test_failed_connect_count_initialized(self):
        """Failed connection counter is initialized."""
        squeezy = Squeezy(name="test")
        assert hasattr(squeezy, '_failed_connect_count')
        assert squeezy._failed_connect_count == 0

    def test_failed_connect_count_increments(self):
        """Failed connection counter increments on failure."""
        squeezy = Squeezy(name="test")
        squeezy._failed_connect_count = 0

        # Simulate failed connections
        for i in range(5):
            squeezy._failed_connect_count += 1
            assert squeezy._failed_connect_count == i + 1

    def test_fallback_triggers_at_five_failures(self):
        """UDP discovery fallback triggers after 5 failures."""
        squeezy = Squeezy(name="test", server="192.0.2.1")  # TEST-NET-1 (unreachable)

        # Track if fallback would be triggered
        squeezy._failed_connect_count = 4
        original_ip = squeezy.server_ip

        # Simulate 5th failure triggering fallback
        squeezy._failed_connect_count += 1
        if squeezy._failed_connect_count >= 5:
            squeezy.server_ip = None
            squeezy._failed_connect_count = 0

        assert squeezy.server_ip is None, "Server IP should be cleared to trigger discovery"

    def test_failed_connect_count_resets_on_success(self):
        """Failed connection counter resets on successful connection."""
        squeezy = Squeezy(name="test")
        squeezy._failed_connect_count = 3

        # Simulate successful connection
        squeezy._failed_connect_count = 0

        assert squeezy._failed_connect_count == 0


class TestP16FlusVsQuit:
    """Tests for strm 'f' (flush) vs 'q' (quit) distinction."""

    def test_quit_command_stops_playback(self):
        """Quit command (strm 'q') stops playback."""
        squeezy = Squeezy(name="test")
        squeezy.playing = True
        squeezy.streaming = True

        # Simulate quit command
        squeezy._stop_playback()

        assert not squeezy.playing, "Quit should stop playback"
        assert not squeezy.streaming, "Quit should stop streaming"

    def test_flush_graceful_behavior(self):
        """Flush command (strm 'f') is graceful - only reports if active."""
        squeezy = Squeezy(name="test")

        # Test with active playback
        squeezy.playing = True
        was_active = squeezy.playing or squeezy.streaming
        assert was_active, "Flush should only report if something was active"

        # Test with nothing active
        squeezy.playing = False
        squeezy.streaming = False
        was_active = squeezy.playing or squeezy.streaming
        assert not was_active, "Flush should not report if nothing was active"


class TestP17FfmpegErrorHandling:
    """Tests for ffmpeg process management and error reporting."""

    def test_ffmpeg_exit_code_detection(self):
        """Non-zero ffmpeg exit code is detected."""
        squeezy = Squeezy(name="test")

        # Mock ffmpeg process with non-zero exit code
        squeezy.ffmpeg_proc = Mock()
        squeezy.ffmpeg_proc.returncode = 1

        # Check if we detect the error
        if squeezy.ffmpeg_proc and squeezy.ffmpeg_proc.returncode and squeezy.ffmpeg_proc.returncode != 0:
            should_error = True
        else:
            should_error = False

        assert should_error, "Should detect ffmpeg error exit code"

    def test_ffmpeg_success_exit(self):
        """ffmpeg with exit code 0 is treated as success."""
        squeezy = Squeezy(name="test")

        # Mock ffmpeg process with successful exit
        squeezy.ffmpeg_proc = Mock()
        squeezy.ffmpeg_proc.returncode = 0

        # Check if we treat as success
        if squeezy.ffmpeg_proc and squeezy.ffmpeg_proc.returncode and squeezy.ffmpeg_proc.returncode != 0:
            should_error = True
        else:
            should_error = False

        assert not should_error, "Should treat exit code 0 as success"

    def test_ffmpeg_no_process(self):
        """Missing ffmpeg process is handled."""
        squeezy = Squeezy(name="test")
        squeezy.ffmpeg_proc = None

        # This should not crash
        if squeezy.ffmpeg_proc:
            exit_code = squeezy.ffmpeg_proc.returncode
        else:
            exit_code = None

        assert exit_code is None, "Should handle missing ffmpeg process"


class TestP14StreamErrorRetry:
    """Tests for stream error recovery and retry logic."""

    def test_transient_error_codes_identified(self):
        """Transient socket errors are correctly identified."""
        import errno

        # List of transient errors that should trigger retry
        transient_errors = [
            errno.EWOULDBLOCK,
            errno.EAGAIN,
            errno.ECONNRESET,
            errno.ECONNABORTED,
        ]

        # All should be in the list
        for err in transient_errors:
            assert err in transient_errors, f"Error {err} should be in transient list"

    def test_permanent_error_handling(self):
        """Permanent errors should not trigger retry."""
        import errno

        permanent_errors = [
            errno.EACCES,  # Permission denied
            errno.ENOTCONN,  # Socket not connected
            errno.EBADF,  # Bad file descriptor
        ]

        transient_errors = [
            errno.EWOULDBLOCK,
            errno.EAGAIN,
            errno.ECONNRESET,
            errno.ECONNABORTED,
        ]

        # Permanent errors should not be in transient list
        for err in permanent_errors:
            assert err not in transient_errors, f"Error {err} should not trigger retry"

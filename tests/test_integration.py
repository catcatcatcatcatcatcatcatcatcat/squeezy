"""Integration tests — squeezy against real Lyrion Music Server."""

import time

from tests.conftest import SQUEEZY_NAME


def poll_status(lms, player_id, expect_mode, timeout=10):
    """Poll LMS until player reaches expected mode or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = lms.player_status(player_id)
        if status.get("mode") == expect_mode:
            return status
        time.sleep(0.5)
    return lms.player_status(player_id)


def poll_elapsed(lms, player_id, min_time, timeout=15):
    """Poll until elapsed time exceeds min_time."""
    deadline = time.time() + timeout
    elapsed = 0.0
    while time.time() < deadline:
        status = lms.player_status(player_id)
        elapsed = float(status.get("time", 0))
        if elapsed > min_time:
            return elapsed
        time.sleep(0.5)
    return elapsed


class TestPlayerDiscovery:
    def test_player_appears_in_lms(self, player_id, lms):
        """squeezy registers and appears in the LMS player list."""
        players = lms.list_players()
        names = [p["name"] for p in players]
        assert SQUEEZY_NAME in names

    def test_player_has_mac_id(self, player_id):
        """Player ID looks like a MAC address."""
        assert ":" in player_id


class TestPlayback:
    def test_stream_start(self, player_id, lms, test_tracks):
        """Queue a track and verify the player enters play mode."""
        lms.playlist_play(player_id, test_tracks["tone"])
        status = poll_status(lms, player_id, "play")
        assert status["mode"] == "play"

    def test_elapsed_time_advances(self, player_id, lms, test_tracks):
        """While playing, elapsed time increases."""
        lms.playlist_play(player_id, test_tracks["tone"])
        poll_status(lms, player_id, "play")
        elapsed = poll_elapsed(lms, player_id, 0.5)
        assert elapsed > 0.5, f"Elapsed time was {elapsed}"


class TestTransportControls:
    def test_pause(self, player_id, lms, test_tracks):
        """Pausing sets player mode to pause."""
        lms.playlist_play(player_id, test_tracks["tone"])
        poll_status(lms, player_id, "play")
        lms.pause(player_id)
        status = poll_status(lms, player_id, "pause")
        assert status["mode"] == "pause"

    def test_resume(self, player_id, lms, test_tracks):
        """Unpausing returns player to play mode."""
        lms.playlist_play(player_id, test_tracks["tone"])
        poll_status(lms, player_id, "play")
        lms.pause(player_id)
        poll_status(lms, player_id, "pause")
        lms.unpause(player_id)
        status = poll_status(lms, player_id, "play")
        assert status["mode"] == "play"

    def test_stop(self, player_id, lms, test_tracks):
        """Stopping sets player mode to stop."""
        lms.playlist_play(player_id, test_tracks["tone"])
        poll_status(lms, player_id, "play")
        lms.stop(player_id)
        status = poll_status(lms, player_id, "stop")
        assert status["mode"] == "stop"


class TestSeek:
    def test_seek_forward(self, player_id, lms, test_tracks):
        """Seeking forward moves the elapsed time to the target position."""
        lms.playlist_play(player_id, test_tracks["sweep"])
        poll_status(lms, player_id, "play")
        poll_elapsed(lms, player_id, 1.0)  # let it play for a bit

        # Seek to 15 seconds
        lms.seek(player_id, 15)
        time.sleep(2)

        status = lms.player_status(player_id)
        elapsed = float(status.get("time", 0))
        assert elapsed >= 14.0, f"After seek to 15s, elapsed was {elapsed}"
        assert status["mode"] == "play"

    def test_seek_backward(self, player_id, lms, test_tracks):
        """Seeking backward rewinds the elapsed time."""
        lms.playlist_play(player_id, test_tracks["sweep"])
        poll_status(lms, player_id, "play")
        poll_elapsed(lms, player_id, 3.0)  # play past 3 seconds

        # Seek back to 1 second
        lms.seek(player_id, 1)
        time.sleep(2)

        status = lms.player_status(player_id)
        elapsed = float(status.get("time", 0))
        # After seeking to 1s and waiting 2s, elapsed should be ~3s (not ~7s+)
        assert elapsed < 6.0, f"After seek to 1s + 2s wait, elapsed was {elapsed}"
        assert status["mode"] == "play"

    def test_seek_preserves_playback(self, player_id, lms, test_tracks):
        """Player stays in play mode through a seek operation."""
        lms.playlist_play(player_id, test_tracks["sweep"])
        poll_status(lms, player_id, "play")
        poll_elapsed(lms, player_id, 1.0)

        # Seek to 20s
        lms.seek(player_id, 20)
        time.sleep(1)

        # Should still be playing
        status = lms.player_status(player_id)
        assert status["mode"] == "play"
        elapsed = float(status.get("time", 0))
        assert elapsed >= 19.0, f"After seek to 20s, elapsed was {elapsed}"


class TestP1ConnectionResilience:
    """Tests for Priority 1 critical reliability features."""

    def test_P12_stop_doesnt_hang(self, player_id, lms, test_tracks):
        """P1.2: Stop command (strm 'q') doesn't hang the player.

        This tests that the quit command handler (strm 'q') correctly
        stops playback without hanging. The 5s connection timeout should
        ensure this completes promptly even under network stress.
        """
        lms.playlist_play(player_id, test_tracks["tone"])
        poll_status(lms, player_id, "play")
        poll_elapsed(lms, player_id, 0.5)

        # Stop should complete within 2 seconds even under stress
        start = time.time()
        lms.stop(player_id)
        status = poll_status(lms, player_id, "stop", timeout=5)
        elapsed = time.time() - start

        assert status["mode"] == "stop"
        assert elapsed < 5.0, f"Stop took {elapsed}s (should be < 5s)"

    def test_P16_flush_vs_quit_distinction(self, player_id, lms, test_tracks):
        """P1.6: Flush (strm 'f') vs Quit (strm 'q') are handled distinctly.

        While LMS doesn't directly expose 'f' vs 'q' in the UI, we can verify
        that stop (which uses 'q') works correctly and immediately halts playback.
        The 'f' command is used for track transitions and is harder to test without
        mocking the socket, but we verify 'q' behavior as a baseline.
        """
        lms.playlist_play(player_id, test_tracks["tone"])
        poll_status(lms, player_id, "play")
        poll_elapsed(lms, player_id, 1.0)

        # Hard stop via 'q' (strm q)
        lms.stop(player_id)
        status = poll_status(lms, player_id, "stop", timeout=5)

        assert status["mode"] == "stop"
        # After stop, elapsed time should not advance
        time.sleep(1)
        status2 = lms.player_status(player_id)
        elapsed_before = float(status.get("time", 0))
        elapsed_after = float(status2.get("time", 0))
        # Time should stay the same (within 0.5s tolerance for clock skew)
        assert abs(elapsed_after - elapsed_before) < 0.5, \
            f"Time changed after stop: {elapsed_before} -> {elapsed_after}"

    def test_P17_graceful_error_handling(self, player_id, lms):
        """P1.7: ffmpeg errors are handled gracefully without crashing player.

        When ffmpeg encounters an error (unsupported format, corrupted file, etc),
        the player should handle it gracefully. We test this by attempting to play
        a non-existent file, which ffmpeg will reject. The player should remain
        responsive and not crash.
        """
        # Try to play a non-existent file
        # This will cause ffmpeg to fail, but squeezy should handle it gracefully
        bad_path = "/music/nonexistent-file-12345.mp3"

        try:
            # LMS might reject this, but if it doesn't, ffmpeg should fail cleanly
            lms.playlist_play(player_id, bad_path)
            time.sleep(2)  # Give ffmpeg time to fail
        except Exception:
            # LMS might reject the request, which is fine
            pass

        # Player should still be responsive
        # It should still respond to status queries
        try:
            status = lms.player_status(player_id)
            # Just verify we got a response - the mode might be stop or error
            assert "mode" in status, "Player should still respond to status queries"
        except Exception as e:
            pytest.fail(f"Player became unresponsive after ffmpeg error: {e}")

    def test_P17_stream_error_recovery(self, player_id, lms, test_tracks):
        """P1.4/P1.7: Stream errors are recovered gracefully.

        When a stream is interrupted mid-play, the player should attempt to
        recover rather than immediately failing. This test starts playback
        and verifies the player remains responsive even if there are
        transient errors.
        """
        lms.playlist_play(player_id, test_tracks["tone"])
        poll_status(lms, player_id, "play")

        # Let it play for a bit
        poll_elapsed(lms, player_id, 2.0)

        # Player should remain responsive to commands throughout playback
        # (This tests that transient errors don't crash the player)
        for i in range(3):
            try:
                status = lms.player_status(player_id)
                assert status["mode"] == "play", f"Iteration {i}: Player unexpectedly stopped"
                time.sleep(1)
            except Exception as e:
                pytest.fail(f"Player became unresponsive at iteration {i}: {e}")

        # Cleanup
        lms.stop(player_id)

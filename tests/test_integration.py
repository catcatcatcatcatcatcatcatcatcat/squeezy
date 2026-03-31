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

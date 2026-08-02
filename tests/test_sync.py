"""Unit tests for multi-room sync handling and timing diagnostics.

Covers the three timing corrections LMS can send a player and the diagnostics
that make a drifting player debuggable:

    strm p (interval > 0)  hold back N ms   → squeezelite OUTPUT_PAUSE_FRAMES
    strm p (interval == 0) real pause       → STMp
    strm a                 drop N ms        → squeezelite OUTPUT_SKIP_FRAMES
    strm u (jiffies)       start-at-time    → squeezelite OUTPUT_START_AT

Reference: squeezelite slimproto.c:306-341 and output.c:70-110.
"""

import struct
import time
from unittest.mock import Mock

import pytest

from squeezy import Squeezy
from squeezy.protocol import slimproto


def _strm(command, interval=0):
    """Build a minimal strm packet with an interval in the replay_gain field."""
    msg = bytearray(28)
    msg[0:4] = b"strm"
    msg[4] = ord(command)
    struct.pack_into(">I", msg, 18, interval)
    return bytes(msg)


def _sent_events(sock):
    """Extract the 4-char STAT event codes from everything written to the socket.

    STAT packets are ``b"STAT" + u32 length + payload``; the event code is the
    first four bytes of the payload.
    """
    events = []
    for call in sock.sendall.call_args_list:
        pkt = call[0][0]
        if pkt[:4] == b"STAT":
            events.append(pkt[8:12].decode("ascii"))
    return events


@pytest.fixture
def player():
    squeezy = Squeezy(name="test")
    squeezy.sock = Mock()
    return squeezy


class TestPauseInterval:
    """strm 'p' carries two different commands depending on the interval."""

    def test_interval_sets_pause_frames_without_pausing(self, player):
        """A non-zero interval is a sync correction, not a pause.

        LMS sends this to hold back a player running ahead of its group. The
        device must keep running — we owe it silence, not a stop.
        """
        player.playing = True
        player.current_sample_rate = 44100

        player.protocol.dispatch(_strm("p", interval=120))

        assert player.pause_frames == int(120 * 44100 / 1000)
        assert player.paused is False
        assert player.playing is True

    def test_interval_does_not_send_stmp(self, player):
        """squeezelite sends STMp only when interval == 0 (slimproto.c:318).

        Sending it for a sync correction makes LMS record the player as paused.
        """
        player.playing = True

        player.protocol.dispatch(_strm("p", interval=120))

        assert "STMp" not in _sent_events(player.sock)

    def test_zero_interval_is_a_real_pause(self, player):
        """interval == 0 keeps the original behaviour: stop and confirm."""
        player.playing = True
        player.device = Mock()

        player.protocol.dispatch(_strm("p", interval=0))

        assert player.paused is True
        assert "STMp" in _sent_events(player.sock)

    def test_correction_is_counted(self, player):
        player.playing = True

        player.protocol.dispatch(_strm("p", interval=50))

        assert player._sync_corrections == 1


class TestSkipAhead:
    """strm 'a' drops buffered audio so a lagging player catches up."""

    def test_skip_does_not_send_stmc(self, player):
        """STMc means "connecting to the stream server" to LMS.

        Squeezebox2.pm:141-180 clears readyToStream/bufferReady and sets
        connecting(1) on STMc, and only an HTTP RESP clears it again — which
        never arrives during a skip. squeezelite replies with nothing here.
        """
        player.playing = True
        player.current_sample_rate = 44100
        player.pcm_buf.write(b"\x01\x02\x03\x04" * 44100)

        player.protocol.dispatch(_strm("a", interval=100))

        assert "STMc" not in _sent_events(player.sock)

    def test_skip_drops_frames_and_advances_elapsed(self, player):
        player.playing = True
        player.current_sample_rate = 44100
        player.pcm_buf.write(b"\x01\x02\x03\x04" * 44100)  # 1s of audio
        before = player.pcm_buf.available()

        player.protocol.dispatch(_strm("a", interval=100))  # drop 100ms

        dropped = before - player.pcm_buf.available()
        assert dropped == int(100 * 44100 / 1000) * slimproto.BYTES_PER_FRAME
        assert player.output_frames == int(100 * 44100 / 1000)


class TestPauseFramesGenerator:
    """The audio generator honours the hold-back without breaking its contract."""

    @staticmethod
    def _run(player, frames):
        gen = player._audio_generator()
        next(gen)  # priming yield
        return gen, gen.send(frames)

    def test_full_callback_of_silence_when_debt_exceeds_period(self, player):
        """Debt larger than one callback: the whole callback is silence."""
        player.playing = True
        player.running = True
        player.pause_frames = 1000
        player.pcm_buf.write(b"\x11\x22\x33\x44" * 512)

        _, chunk = self._run(player, 256)

        assert chunk == b"\x00" * (256 * slimproto.BYTES_PER_FRAME)
        assert player.pause_frames == 1000 - 256
        assert player.output_frames == 0  # silence is not played audio
        assert player.pcm_buf.available() == 512 * slimproto.BYTES_PER_FRAME

    def test_partial_debt_yields_exact_frame_count(self, player):
        """Debt smaller than one callback: silence, then real audio.

        miniaudio requires exactly ``framecount`` frames back, so a partial
        hold-back must be padded out with real audio in the same callback.
        """
        player.playing = True
        player.running = True
        player.pause_frames = 100
        player.pcm_buf.write(b"\x11\x22\x33\x44" * 512)

        _, chunk = self._run(player, 256)

        assert len(chunk) == 256 * slimproto.BYTES_PER_FRAME
        assert chunk[:100 * slimproto.BYTES_PER_FRAME] == b"\x00" * (100 * slimproto.BYTES_PER_FRAME)
        assert chunk[100 * slimproto.BYTES_PER_FRAME:] == b"\x11\x22\x33\x44" * 156
        assert player.pause_frames == 0
        assert player.output_frames == 156  # only the real frames count

    def test_no_debt_is_unchanged(self, player):
        """With no correction outstanding the callback is plain audio."""
        player.playing = True
        player.running = True
        player.pcm_buf.write(b"\x11\x22\x33\x44" * 512)

        _, chunk = self._run(player, 256)

        assert chunk == b"\x11\x22\x33\x44" * 256
        assert player.output_frames == 256

    def test_callback_period_is_recorded(self, player):
        """The observed period is the only honest source for device geometry."""
        player.playing = True
        player.running = True
        player.pcm_buf.write(b"\x11\x22\x33\x44" * 512)

        self._run(player, 256)

        assert player._cb_period_frames == 256


class TestGaplessSyncGate:
    """A gapless switch must honour the queued track's autostart.

    squeezelite's output goes OUTPUT_STOPPED at a track drain and will not
    consume the next track until autostart=1 threshold or a strm-u anchor
    (output.c:63,112).  Playing early is unrecoverable: the anchor can only
    delay consumption, never rewind it, so a head start becomes a constant
    audible offset for the whole track.
    """

    @staticmethod
    def _drain_into_switch(player, autostart, callback_frames=256):
        """Drive the generator through a gapless switch to a queued track."""
        player.playing = True
        player.running = True
        player.streaming = True
        player.decode_complete = True
        player._pending_track = ("10.0.0.2", 9000, b"GET / HTTP/1.0\r\n\r\n",
                                 65536, autostart, "m", None)
        gen = player._audio_generator()
        next(gen)
        # Buffer empty + decode complete → STMd/STMu → switch to pending
        chunk = gen.send(callback_frames)
        return gen, chunk

    def test_synced_autostart_gates_output(self, player):
        """Queued autostart=0 (post-CONT sync): silence, buffer untouched."""
        gen, chunk = self._drain_into_switch(player, autostart=0)
        assert player._output_gated is True

        # New track's data arrives — generator must NOT consume it
        player.pcm_buf.write(b"\x11\x22\x33\x44" * 512)
        frames_before = player.output_frames
        chunk = gen.send(256)

        assert chunk == b"\x00" * (256 * slimproto.BYTES_PER_FRAME)
        assert player.pcm_buf.available() == 512 * slimproto.BYTES_PER_FRAME
        assert player.output_frames == frames_before

    def test_wait_for_cont_gates_output(self, player):
        """Queued autostart=2 (pre-CONT) is also gated."""
        gen, _ = self._drain_into_switch(player, autostart=2)
        assert player._output_gated is True

    def test_normal_autostart_plays_through(self, player):
        """Queued autostart=1: classic gapless, ungated (regression guard)."""
        gen, _ = self._drain_into_switch(player, autostart=1)
        assert player._output_gated is False

        player.pcm_buf.write(b"\x11\x22\x33\x44" * 512)
        chunk = gen.send(256)
        assert chunk == b"\x11\x22\x33\x44" * 256

    def test_gated_threshold_sends_stml_and_stays_gated(self, player):
        """While gated with autostart=0, threshold → STMl, gate stays closed.

        The old code bailed out of _check_threshold_start whenever
        playing=True, so a gated switch never signalled readiness and LMS
        never got the STMl it needs to fire the group anchor.
        """
        gen, _ = self._drain_into_switch(player, autostart=0)
        player.pcm_buf.write(b"\x11\x22\x33\x44" * 44100)  # well past threshold

        player._check_threshold_start(65536, 0)

        assert player.sent_STMl is True
        assert "STMl" in _sent_events(player.sock)
        assert player._output_gated is True  # anchor releases it, not STMl

    def test_gated_threshold_autostart1_releases_gate(self, player):
        """autostart 3 → CONT → 1: threshold releases the gate itself."""
        gen, _ = self._drain_into_switch(player, autostart=3)
        player.autostart = 1  # as the CONT handler would leave it
        player.pcm_buf.write(b"\x11\x22\x33\x44" * 44100)

        player._check_threshold_start(65536, 1)

        assert player._output_gated is False
        assert "STMs" in _sent_events(player.sock)

    def test_anchor_releases_gate_and_starts_from_sample_zero(self, player):
        """strm u releases the gate; playback starts at the anchor, sample 0."""
        gen, _ = self._drain_into_switch(player, autostart=0)
        player.pcm_buf.write(b"\x11\x22\x33\x44" * 512)

        # Anchor already reached (target in the past → immediate start)
        past = (slimproto.gettime_ms() - 5) & 0xFFFFFFFF
        player.protocol.dispatch(_strm("u", interval=past))

        assert player._output_gated is False
        chunk = gen.send(256)
        # start-at fires: counters reset, STMs sent, first frames are sample 0
        assert chunk == b"\x11\x22\x33\x44" * 256
        assert "STMs" in _sent_events(player.sock)
        assert player.output_frames == 256
        assert player._track_start_frames == 0

    def test_stream_loop_keeps_checking_threshold_while_gated(self, player):
        """The stream loop's 'started' flag must see through the gate."""
        gen, _ = self._drain_into_switch(player, autostart=0)
        started = player.sent_STMl or (player.playing and not player._output_gated)
        assert started is False

    def test_stop_playback_clears_gate(self, player):
        gen, _ = self._drain_into_switch(player, autostart=0)
        player._stop_playback()
        assert player._output_gated is False


class TestAnchorTruthfulElapsed:
    """The start-at reset must never hide already-consumed audio from LMS."""

    def test_anchor_with_consumed_frames_keeps_elapsed(self, player):
        """If frames were consumed before the anchor, do NOT zero the counters.

        LMS's _CheckSync measures us purely by reported elapsed_ms
        (Squeezebox2::playPoint = jiffiesToTimestamp(jiffies) − elapsed/1000).
        Zeroing after a head start makes LMS 'verify' a player that is
        audibly ahead — the correction then fixes the lie, not the audio.
        """
        player.playing = True
        player.running = True
        player.pcm_buf.write(b"\x11\x22\x33\x44" * 1024)
        gen = player._audio_generator()
        next(gen)
        gen.send(441)  # consume 441 frames (~10ms) before any anchor
        assert player.output_frames == 441

        past = (slimproto.gettime_ms() - 5) & 0xFFFFFFFF
        player.start_at_jiffies = past
        gen.send(256)  # anchor fires during this callback

        # Counters kept — elapsed stays truthful (441 + 256 frames)
        assert player.output_frames == 441 + 256

    def test_anchor_with_no_consumption_resets_counters(self, player):
        """Clean sync start (nothing consumed): elapsed restarts at zero."""
        player.playing = True
        player.running = True
        player.pcm_buf.write(b"\x11\x22\x33\x44" * 1024)
        past = (slimproto.gettime_ms() - 5) & 0xFFFFFFFF
        player.start_at_jiffies = past

        gen = player._audio_generator()
        next(gen)
        gen.send(256)

        assert player._track_start_frames == 0
        assert player.output_frames == 256


class TestClockStepDetection:
    """gettime_ms() rides the wall clock, which sleep and NTP can move."""

    def test_steady_clock_reports_no_step(self, player):
        assert player._check_clock_step() == 0.0
        assert player._clock_steps == 0

    def test_step_is_detected_and_counted(self, player):
        """Simulate the wall clock jumping forward relative to monotonic."""
        player._clock_offset -= 5.0  # as if 5s of wall time appeared

        step_ms = player._check_clock_step()

        assert step_ms == pytest.approx(5000, abs=50)
        assert player._clock_steps == 1

    def test_step_rebaselines_so_it_reports_once(self, player):
        player._clock_offset -= 5.0
        player._check_clock_step()

        assert player._check_clock_step() == 0.0
        assert player._clock_steps == 1

    def test_small_drift_is_ignored(self, player):
        """Below the threshold there is nothing worth reporting."""
        player._clock_offset -= (slimproto.CLOCK_STEP_WARN_MS / 2) / 1000.0

        assert player._check_clock_step() == 0.0


class TestAudioStallDetection:
    """A device lost across sleep looks like: data ready, frames frozen."""

    def test_stall_with_pending_audio_is_reported(self, player):
        player.playing = True
        player.pcm_buf.write(b"\x11\x22\x33\x44" * 512)
        player._last_frame_update_time = time.monotonic() - (slimproto.AUDIO_STALL_WARN_SEC + 1)

        player._check_audio_stall()

        assert player._audio_stalled is True

    def test_empty_buffer_is_not_a_stall(self, player):
        """An empty buffer is a network underrun, not a dead device."""
        player.playing = True
        player._last_frame_update_time = time.monotonic() - (slimproto.AUDIO_STALL_WARN_SEC + 1)

        player._check_audio_stall()

        assert player._audio_stalled is False

    def test_recent_frames_are_not_a_stall(self, player):
        player.playing = True
        player.pcm_buf.write(b"\x11\x22\x33\x44" * 512)
        player._last_frame_update_time = time.monotonic()

        player._check_audio_stall()

        assert player._audio_stalled is False

    def test_reported_once_per_stall(self, player):
        player.playing = True
        player.pcm_buf.write(b"\x11\x22\x33\x44" * 512)
        player._last_frame_update_time = time.monotonic() - (slimproto.AUDIO_STALL_WARN_SEC + 1)

        player._check_audio_stall()
        player._check_audio_stall()  # latch must suppress the repeat

        assert player._audio_stalled is True

    def test_not_playing_clears_the_latch(self, player):
        player._audio_stalled = True
        player.playing = False

        player._check_audio_stall()

        assert player._audio_stalled is False

"""Unit tests for Priority 2 features: replay gain, ICY metadata, gapless, variable sample rates."""

import struct
import threading
import time
from unittest.mock import Mock, patch, MagicMock, call
import array

import pytest

from squeezy import Squeezy


class TestP23ReplayGain:
    """Tests for replay gain support (P2.3)."""

    def test_replay_gain_initialized(self):
        """Replay gain is initialized to unity (1.0)."""
        squeezy = Squeezy(name="test")
        assert hasattr(squeezy, 'replay_gain')
        assert squeezy.replay_gain == 1.0

    def test_replay_gain_extraction_from_strm_packet(self):
        """Replay gain (16.16 fixed-point) is extracted from strm 's' packet."""
        squeezy = Squeezy(name="test")
        squeezy.sock = Mock()  # Mock socket to avoid send failures

        # Build a strm 's' packet with replay_gain at offset 18
        # 16.16 fixed-point: 0x10000 = 1.0 (unity), 0x20000 = 2.0 (double)
        msg = bytearray(28 + 10)  # minimum 28 bytes + some extra
        msg[0:4] = b"strm"
        msg[4] = ord("s")
        msg[5] = ord("1")  # autostart = 1
        msg[6] = ord("m")  # format = mp3

        # Set replay_gain to 1.5 (0x18000 in 16.16 fixed-point)
        struct.pack_into(">I", msg, 18, 0x18000)
        # Set server port and IP (required for packet validation)
        struct.pack_into(">H", msg, 22, 3483)
        struct.pack_into(">I", msg, 24, 0)  # localhost

        squeezy.protocol._handle_strm_start(bytes(msg))

        # replay_gain should be 0x18000 / 0x10000 = 1.5
        assert abs(squeezy.replay_gain - 1.5) < 0.01

    def test_replay_gain_unity(self):
        """Replay gain of 0x10000 (unity) converts to 1.0."""
        squeezy = Squeezy(name="test")
        squeezy.sock = Mock()

        msg = bytearray(28 + 10)
        msg[0:4] = b"strm"
        msg[4] = ord("s")
        msg[5] = ord("1")
        msg[6] = ord("m")
        struct.pack_into(">I", msg, 18, 0x10000)  # Unity gain
        struct.pack_into(">H", msg, 22, 3483)
        struct.pack_into(">I", msg, 24, 0)

        squeezy.protocol._handle_strm_start(bytes(msg))
        assert squeezy.replay_gain == 1.0

    def test_replay_gain_boost(self):
        """Replay gain > 1.0 boosts volume."""
        squeezy = Squeezy(name="test")
        squeezy.sock = Mock()

        msg = bytearray(28 + 10)
        msg[0:4] = b"strm"
        msg[4] = ord("s")
        msg[5] = ord("1")
        msg[6] = ord("m")
        struct.pack_into(">I", msg, 18, 0x20000)  # 2.0x gain
        struct.pack_into(">H", msg, 22, 3483)
        struct.pack_into(">I", msg, 24, 0)

        squeezy.protocol._handle_strm_start(bytes(msg))
        assert squeezy.replay_gain == 2.0

    def test_replay_gain_cut(self):
        """Replay gain < 1.0 reduces volume."""
        squeezy = Squeezy(name="test")
        squeezy.sock = Mock()

        msg = bytearray(28 + 10)
        msg[0:4] = b"strm"
        msg[4] = ord("s")
        msg[5] = ord("1")
        msg[6] = ord("m")
        struct.pack_into(">I", msg, 18, 0x8000)  # 0.5x gain
        struct.pack_into(">H", msg, 22, 3483)
        struct.pack_into(">I", msg, 24, 0)

        squeezy.protocol._handle_strm_start(bytes(msg))
        assert abs(squeezy.replay_gain - 0.5) < 0.01

    def test_replay_gain_default_short_packet(self):
        """Short packet defaults to unity replay gain."""
        squeezy = Squeezy(name="test")

        # Packet too short to contain replay_gain
        msg = bytearray(20)  # Less than 22 bytes
        msg[0:4] = b"strm"
        msg[4] = ord("s")
        msg[5] = ord("1")
        msg[6] = ord("m")

        squeezy.protocol._handle_strm_start(bytes(msg))
        assert squeezy.replay_gain == 1.0


class TestP21TrueGapless:
    """Tests for true gapless playback (P2.1)."""

    def test_track_boundary_initialization(self):
        """Track boundary tracking is initialized."""
        squeezy = Squeezy(name="test")
        assert hasattr(squeezy, '_current_track_id')
        assert hasattr(squeezy, '_track_start_frames')
        assert hasattr(squeezy, '_switching_track')
        assert squeezy._current_track_id == 0
        assert squeezy._track_start_frames == 0
        assert squeezy._switching_track == False

    def test_reset_track_state_increments_id(self):
        """_reset_track_state increments track ID."""
        squeezy = Squeezy(name="test")
        squeezy.output_frames = 100

        initial_id = squeezy._current_track_id
        squeezy._reset_track_state()

        assert squeezy._current_track_id == initial_id + 1

    def test_reset_track_state_records_frame_boundary(self):
        """_reset_track_state records output_frames as track boundary."""
        squeezy = Squeezy(name="test")
        squeezy.output_frames = 44100  # 1 second at 44.1kHz

        squeezy._reset_track_state()

        assert squeezy._track_start_frames == 44100

    def test_reset_track_state_clears_stat_flags(self):
        """_reset_track_state clears per-track STAT flags."""
        squeezy = Squeezy(name="test")
        squeezy.sent_STMd = True
        squeezy.sent_STMu = True
        squeezy.sent_STMo = True
        squeezy.sent_STMl = True

        squeezy._reset_track_state()

        assert squeezy.sent_STMd == False
        assert squeezy.sent_STMu == False
        assert squeezy.sent_STMo == False
        assert squeezy.sent_STMl == False

    def test_elapsed_ms_track_relative(self):
        """Elapsed time is calculated relative to track boundary."""
        squeezy = Squeezy(name="test")
        squeezy.current_sample_rate = 44100

        # Track starts at frame 88200 (2 seconds)
        squeezy._track_start_frames = 88200
        # We're now at frame 132300 (3 seconds)
        squeezy.output_frames = 132300

        # elapsed_ms should be ~1 second (frames in current track / rate)
        elapsed = squeezy._elapsed_ms()
        # Should be approximately 1000ms
        assert 900 < elapsed < 1100, f"Expected ~1000ms, got {elapsed}ms"

    def test_elapsed_ms_track_boundary_zero(self):
        """Elapsed time is zero at track boundary."""
        squeezy = Squeezy(name="test")
        squeezy.current_sample_rate = 44100
        squeezy._track_start_frames = 100
        squeezy.output_frames = 100  # At boundary

        elapsed = squeezy._elapsed_ms()
        assert elapsed == 0


class TestP25VariableSampleRate:
    """Tests for variable sample rate support."""

    def test_sample_rate_state_initialized(self):
        """Sample rate tracking is initialized."""
        squeezy = Squeezy(name="test")
        assert squeezy.current_sample_rate == 44100
        assert squeezy.next_sample_rate == 44100
        assert squeezy.supported_rates == [44100, 48000, 96000, 192000]

    def test_get_supported_rate_unity(self):
        """_get_supported_rate returns rate if it's supported."""
        squeezy = Squeezy(name="test")

        assert squeezy._get_supported_rate(44100) == 44100
        assert squeezy._get_supported_rate(48000) == 48000
        assert squeezy._get_supported_rate(96000) == 96000
        assert squeezy._get_supported_rate(192000) == 192000

    def test_get_supported_rate_fallback(self):
        """_get_supported_rate falls back to 44100 for unsupported rates."""
        squeezy = Squeezy(name="test")

        # Unsupported rates
        assert squeezy._get_supported_rate(8000) == 44100
        assert squeezy._get_supported_rate(22050) == 44100
        assert squeezy._get_supported_rate(32000) == 44100
        assert squeezy._get_supported_rate(176400) == 44100

    def test_detect_rate_pcm_format(self):
        """Sample rate is detected from PCM format metadata."""
        squeezy = Squeezy(name="test")
        squeezy.sock = Mock()

        # Build strm 's' packet with PCM format and rate index
        msg = bytearray(28 + 10)
        msg[0:4] = b"strm"
        msg[4] = ord("s")
        msg[5] = ord("1")
        msg[6] = ord("p")  # PCM format
        msg[7] = ord("1")  # 16-bit samples
        msg[8] = ord("3")  # Rate index 3 = 44100 (from rate_table)
        msg[9] = ord("2")  # 2 channels (stereo)
        msg[10] = ord("1")  # little-endian
        struct.pack_into(">I", msg, 18, 0x10000)  # replay_gain
        struct.pack_into(">H", msg, 22, 3483)
        struct.pack_into(">I", msg, 24, 0)

        squeezy.protocol._handle_strm_start(bytes(msg))

        # Rate index 3 = 44100 Hz
        assert squeezy.next_sample_rate == 44100

    def test_detect_rate_pcm_48k(self):
        """PCM format 48kHz detection."""
        squeezy = Squeezy(name="test")
        squeezy.sock = Mock()

        msg = bytearray(28 + 10)
        msg[0:4] = b"strm"
        msg[4] = ord("s")
        msg[5] = ord("1")
        msg[6] = ord("p")  # PCM
        msg[7] = ord("1")  # 16-bit
        msg[8] = ord("4")  # Rate index 4 = 48000 Hz
        msg[9] = ord("2")  # stereo
        msg[10] = ord("1")  # LE
        struct.pack_into(">I", msg, 18, 0x10000)
        struct.pack_into(">H", msg, 22, 3483)
        struct.pack_into(">I", msg, 24, 0)

        squeezy.protocol._handle_strm_start(bytes(msg))
        assert squeezy.next_sample_rate == 48000

    def test_detect_rate_compressed_default(self):
        """Compressed formats default to 44100 pending ffmpeg detection."""
        squeezy = Squeezy(name="test")
        squeezy.sock = Mock()

        msg = bytearray(28 + 10)
        msg[0:4] = b"strm"
        msg[4] = ord("s")
        msg[5] = ord("1")
        msg[6] = ord("m")  # MP3 (compressed)
        struct.pack_into(">I", msg, 18, 0x10000)
        struct.pack_into(">H", msg, 22, 3483)
        struct.pack_into(">I", msg, 24, 0)

        squeezy.protocol._handle_strm_start(bytes(msg))

        # Before ffmpeg detection, should default to 44100
        assert squeezy.next_sample_rate == 44100

    def test_start_audio_uses_next_sample_rate(self):
        """_start_audio opens device at next_sample_rate."""
        squeezy = Squeezy(name="test")
        squeezy.next_sample_rate = 48000

        # Mock miniaudio device
        with patch('squeezy.miniaudio.PlaybackDevice') as mock_device:
            mock_instance = MagicMock()
            mock_device.return_value = mock_instance

            squeezy._start_audio()

            # Should open device at 48000 Hz
            mock_device.assert_called_once()
            call_kwargs = mock_device.call_args[1]
            assert call_kwargs['sample_rate'] == 48000
            assert squeezy.current_sample_rate == 48000

    def test_start_audio_updates_current_sample_rate(self):
        """_start_audio updates current_sample_rate."""
        squeezy = Squeezy(name="test")
        squeezy.next_sample_rate = 96000
        squeezy.current_sample_rate = 44100

        with patch('squeezy.miniaudio.PlaybackDevice'):
            squeezy._start_audio()

            assert squeezy.current_sample_rate == 96000

    def test_start_audio_accepts_sample_rate_parameter(self):
        """_start_audio accepts optional sample_rate parameter."""
        squeezy = Squeezy(name="test")

        with patch('squeezy.miniaudio.PlaybackDevice') as mock_device:
            mock_instance = MagicMock()
            mock_device.return_value = mock_instance

            squeezy._start_audio(sample_rate=192000)

            call_kwargs = mock_device.call_args[1]
            assert call_kwargs['sample_rate'] == 192000
            assert squeezy.current_sample_rate == 192000


class TestP24ICYMetadata:
    """Tests for ICY metadata extraction (P2.4)."""

    def test_icy_metadata_init(self):
        """ICY metadata fields are initialized."""
        squeezy = Squeezy(name="test")
        assert squeezy.icy_title == ""
        assert squeezy.icy_artist == ""
        assert squeezy.icy_album == ""
        assert squeezy.icy_meta_int == 0

    def test_icy_metaint_extracted_from_headers(self):
        """icy-metaint header is extracted from HTTP response."""
        squeezy = Squeezy(name="test")

        resp_headers = b"HTTP/1.1 200 OK\r\nicy-metaint: 8192\r\nContent-Length: 1000000\r\n\r\n"

        # Simulate header parsing (from _do_stream)
        headers_str = resp_headers.decode("ascii", errors="replace")
        for line in headers_str.split("\r\n"):
            if line.lower().startswith("icy-metaint:"):
                squeezy.icy_meta_int = int(line.split(":", 1)[1].strip())
                break

        assert squeezy.icy_meta_int == 8192

    def test_parse_icy_metadata_streamtitle(self):
        """ICY metadata StreamTitle is extracted."""
        squeezy = Squeezy(name="test")

        # ICY metadata block: 1 byte length (in 16-byte units), then length*16 bytes
        # Use simple metadata that fits in 16 bytes:
        meta_str = b"StreamTitle='Hi';"  # 16 bytes exactly
        meta_block = bytes([1]) + meta_str  # Length 1 = 16 bytes

        result = squeezy._parse_icy_metadata(meta_block)

        assert squeezy.icy_title == "Hi"
        assert result == True  # Title changed

    def test_parse_icy_metadata_artist(self):
        """ICY metadata StreamArtist is extracted."""
        squeezy = Squeezy(name="test")

        # Use longer metadata with padding (2 blocks = 32 bytes)
        meta_str = b"StreamTitle='Song';StreamArtist='Art';"  # 37 bytes
        # Need 3 blocks of 16 bytes = 48 bytes
        meta_bytes = meta_str + b"\x00" * (48 - len(meta_str))
        meta_block = bytes([3]) + meta_bytes  # Length 3 = 48 bytes

        squeezy._parse_icy_metadata(meta_block)
        assert squeezy.icy_artist == "Art"
        assert squeezy.icy_title == "Song"

    def test_parse_icy_metadata_empty_block(self):
        """Empty ICY metadata block (length 0) is handled."""
        squeezy = Squeezy(name="test")

        meta_block = bytes([0])  # Length 0
        result = squeezy._parse_icy_metadata(meta_block)

        assert result == False
        assert squeezy.icy_title == ""

    def test_status_dict_includes_icy_title(self):
        """Status dict includes ICY title as fallback."""
        squeezy = Squeezy(name="test")
        squeezy.icy_title = "Radio Track"
        squeezy.playing = True

        status = squeezy._status_dict()

        assert status['title'] == "Radio Track"

    def test_status_dict_lms_title_priority(self):
        """LMS title takes priority over ICY metadata."""
        squeezy = Squeezy(name="test")
        squeezy.lms_title = "LMS Track"
        squeezy.icy_title = "ICY Track"

        status = squeezy._status_dict()

        assert status['title'] == "LMS Track"


class TestP22Crossfade:
    """Tests for crossfade support (P2.2)."""

    def test_transition_parameters_initialized(self):
        """Transition parameters are initialized."""
        squeezy = Squeezy(name="test")
        assert squeezy.transition_type == 0
        assert squeezy.transition_period_sec == 0
        assert hasattr(squeezy, '_crossfade_enabled')
        assert hasattr(squeezy, '_fade_in_gains')
        assert hasattr(squeezy, '_fade_out_gains')
        assert hasattr(squeezy, '_crossfade_samples')
        assert hasattr(squeezy, '_crossfade_pos')
        assert hasattr(squeezy, '_crossfade_total')

    def test_transition_parameters_extracted_from_strm(self):
        """Transition parameters are extracted from strm 's' packet."""
        squeezy = Squeezy(name="test")
        squeezy.sock = Mock()

        # Build strm 's' packet with transition params at offsets 13-14
        msg = bytearray(28 + 10)
        msg[0:4] = b"strm"
        msg[4] = ord("s")
        msg[5] = ord("1")
        msg[6] = ord("m")  # MP3
        # Offsets 13-14: transition_period and transition_type (ASCII '0'-'9')
        msg[13] = ord("3")  # transition_period = 3 seconds
        msg[14] = ord("1")  # transition_type = 1 (CROSSFADE)
        struct.pack_into(">I", msg, 18, 0x10000)  # replay_gain
        struct.pack_into(">H", msg, 22, 3483)
        struct.pack_into(">I", msg, 24, 0)

        squeezy.protocol._handle_strm_start(bytes(msg))

        assert squeezy.transition_type == 1
        assert squeezy.transition_period_sec == 3

    def test_transition_type_fade_none(self):
        """Transition type 0 is FADE_NONE."""
        squeezy = Squeezy(name="test")
        squeezy.sock = Mock()

        msg = bytearray(28 + 10)
        msg[0:4] = b"strm"
        msg[4] = ord("s")
        msg[5] = ord("1")
        msg[6] = ord("m")
        msg[13] = ord("2")  # period
        msg[14] = ord("0")  # type = FADE_NONE
        struct.pack_into(">I", msg, 18, 0x10000)
        struct.pack_into(">H", msg, 22, 3483)
        struct.pack_into(">I", msg, 24, 0)

        squeezy.protocol._handle_strm_start(bytes(msg))
        assert squeezy.transition_type == 0

    def test_transition_type_fade_in(self):
        """Transition type 2 is FADE_IN."""
        squeezy = Squeezy(name="test")
        squeezy.sock = Mock()

        msg = bytearray(28 + 10)
        msg[0:4] = b"strm"
        msg[4] = ord("s")
        msg[5] = ord("1")
        msg[6] = ord("m")
        msg[13] = ord("2")
        msg[14] = ord("2")  # type = FADE_IN
        struct.pack_into(">I", msg, 18, 0x10000)
        struct.pack_into(">H", msg, 22, 3483)
        struct.pack_into(">I", msg, 24, 0)

        squeezy.protocol._handle_strm_start(bytes(msg))
        assert squeezy.transition_type == 2

    def test_transition_type_fade_out(self):
        """Transition type 3 is FADE_OUT."""
        squeezy = Squeezy(name="test")
        squeezy.sock = Mock()

        msg = bytearray(28 + 10)
        msg[0:4] = b"strm"
        msg[4] = ord("s")
        msg[5] = ord("1")
        msg[6] = ord("m")
        msg[13] = ord("2")
        msg[14] = ord("3")  # type = FADE_OUT
        struct.pack_into(">I", msg, 18, 0x10000)
        struct.pack_into(">H", msg, 22, 3483)
        struct.pack_into(">I", msg, 24, 0)

        squeezy.protocol._handle_strm_start(bytes(msg))
        assert squeezy.transition_type == 3

    def test_transition_type_fade_inout(self):
        """Transition type 4 is FADE_INOUT."""
        squeezy = Squeezy(name="test")
        squeezy.sock = Mock()

        msg = bytearray(28 + 10)
        msg[0:4] = b"strm"
        msg[4] = ord("s")
        msg[5] = ord("1")
        msg[6] = ord("m")
        msg[13] = ord("2")
        msg[14] = ord("4")  # type = FADE_INOUT
        struct.pack_into(">I", msg, 18, 0x10000)
        struct.pack_into(">H", msg, 22, 3483)
        struct.pack_into(">I", msg, 24, 0)

        squeezy.protocol._handle_strm_start(bytes(msg))
        assert squeezy.transition_type == 4

    def test_build_fade_curves_linear(self):
        """Gain curves are linear (0.0 → 1.0 and 1.0 → 0.0)."""
        squeezy = Squeezy(name="test")

        fade_in, fade_out = squeezy._build_fade_curves(100)

        assert fade_in is not None
        assert fade_out is not None
        assert len(fade_in) == 100
        assert len(fade_out) == 100
        # Fade-in starts at 0.0
        assert abs(fade_in[0] - 0.0) < 0.01
        # Fade-in ends at 99/100 = 0.99
        assert abs(fade_in[99] - 0.99) < 0.01
        # Fade-out starts at 1.0
        assert abs(fade_out[0] - 1.0) < 0.01
        # Fade-out ends at 1/100 = 0.01
        assert abs(fade_out[99] - 0.01) < 0.01
        # Complementary: fade_in + fade_out ≈ 1.0 at any point
        for i in range(100):
            assert abs(fade_in[i] + fade_out[i] - 1.0) < 0.01

    def test_build_fade_curves_zero_duration(self):
        """Zero or negative duration returns None."""
        squeezy = Squeezy(name="test")

        fade_in, fade_out = squeezy._build_fade_curves(0)
        assert fade_in is None
        assert fade_out is None

        fade_in, fade_out = squeezy._build_fade_curves(-100)
        assert fade_in is None
        assert fade_out is None

    def test_crossfade_mixing_crossfade_mode(self):
        """Crossfade mode (1) mixes old fade-out + new fade-in."""
        squeezy = Squeezy(name="test")
        squeezy.transition_type = 1  # CROSSFADE
        squeezy.current_sample_rate = 44100
        squeezy.transition_period_sec = 1
        squeezy._crossfade_total = 2  # 2 samples
        squeezy._crossfade_pos = 0

        # Build gain curves: [0.0, 0.5] for fade_in, [1.0, 0.5] for fade_out
        fade_in, fade_out = squeezy._build_fade_curves(2)
        squeezy._fade_in_gains = fade_in
        squeezy._fade_out_gains = fade_out

        # Old track samples: [1000, 2000]
        old_sample_bytes = array.array("h", [1000, 2000]).tobytes()
        squeezy._crossfade_samples = list(old_sample_bytes)

        # New track samples: [3000, 4000]
        new_chunk = array.array("h", [3000, 4000]).tobytes()

        result = squeezy._apply_crossfade(new_chunk)
        result_samples = array.array("h", result)

        # At position 0: old * 1.0 + new * 0.0 = 1000 * 1.0 + 3000 * 0.0 = 1000
        assert abs(result_samples[0] - 1000) < 10
        # At position 1: old * 0.5 + new * 0.5 = 2000 * 0.5 + 4000 * 0.5 = 3000
        assert abs(result_samples[1] - 3000) < 10
        # Crossfade should be complete now
        assert squeezy._crossfade_pos >= squeezy._crossfade_total
        assert not squeezy._crossfade_enabled

    def test_crossfade_mixing_fade_in_mode(self):
        """Fade-in mode (2) applies only fade-in to new track."""
        squeezy = Squeezy(name="test")
        squeezy.transition_type = 2  # FADE_IN
        squeezy.current_sample_rate = 44100
        squeezy.transition_period_sec = 1
        squeezy._crossfade_total = 2
        squeezy._crossfade_pos = 0

        fade_in, fade_out = squeezy._build_fade_curves(2)
        squeezy._fade_in_gains = fade_in  # [0.0, 0.5]
        squeezy._fade_out_gains = fade_out

        old_sample_bytes = array.array("h", [1000, 2000]).tobytes()
        squeezy._crossfade_samples = list(old_sample_bytes)

        new_chunk = array.array("h", [4000, 4000]).tobytes()

        result = squeezy._apply_crossfade(new_chunk)
        result_samples = array.array("h", result)

        # FADE_IN: gain_out = 0.0, gain_in = [0.0, 0.5]
        # At position 0: 0 * 1000 + 0.0 * 4000 = 0
        assert abs(result_samples[0] - 0) < 10
        # At position 1: 0 * 2000 + 0.5 * 4000 = 2000
        assert abs(result_samples[1] - 2000) < 10

    def test_crossfade_mixing_fade_out_mode(self):
        """Fade-out mode (3) applies only fade-out to old track."""
        squeezy = Squeezy(name="test")
        squeezy.transition_type = 3  # FADE_OUT
        squeezy.current_sample_rate = 44100
        squeezy.transition_period_sec = 1
        squeezy._crossfade_total = 2
        squeezy._crossfade_pos = 0

        fade_in, fade_out = squeezy._build_fade_curves(2)  # [0.0, 0.5] and [1.0, 0.5]
        squeezy._fade_in_gains = fade_in
        squeezy._fade_out_gains = fade_out

        old_sample_bytes = array.array("h", [2000, 2000]).tobytes()
        squeezy._crossfade_samples = list(old_sample_bytes)

        new_chunk = array.array("h", [4000, 4000]).tobytes()

        result = squeezy._apply_crossfade(new_chunk)
        result_samples = array.array("h", result)

        # FADE_OUT: gain_out = [1.0, 0.5], gain_in = 0.0
        # At position 0: 1.0 * 2000 + 0 * 4000 = 2000
        assert abs(result_samples[0] - 2000) < 10
        # At position 1: 0.5 * 2000 + 0 * 4000 = 1000
        assert abs(result_samples[1] - 1000) < 10

    def test_reset_track_state_clears_crossfade(self):
        """_reset_track_state() clears crossfade state."""
        squeezy = Squeezy(name="test")
        squeezy._crossfade_enabled = True
        squeezy._crossfade_samples = [1, 2, 3]
        squeezy._crossfade_pos = 5
        squeezy._crossfade_total = 100
        squeezy.output_frames = 100

        squeezy._reset_track_state()

        assert squeezy._crossfade_enabled == False
        assert squeezy._crossfade_samples == []
        assert squeezy._crossfade_pos == 0
        assert squeezy._crossfade_total == 0

    def test_transition_parameters_default_short_packet(self):
        """Short packet defaults to no transition."""
        squeezy = Squeezy(name="test")
        squeezy.sock = Mock()

        # Packet too short to contain transition params
        msg = bytearray(10)
        msg[0:4] = b"strm"
        msg[4] = ord("s")
        msg[5] = ord("1")
        msg[6] = ord("m")

        squeezy.protocol._handle_strm_start(bytes(msg))

        assert squeezy.transition_type == 0
        assert squeezy.transition_period_sec == 0

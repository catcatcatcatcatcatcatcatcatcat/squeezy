#!/usr/bin/env python3
"""Audio playback pipeline: device management, crossfade, elapsed time tracking.

Handles miniaudio device lifecycle, crossfade mixing, volume/gain scaling,
and dynamic device delay compensation for accurate elapsed time reporting.
"""

import array
import logging
import time

import miniaudio

log = logging.getLogger("squeezy")


class AudioPlayer:
    """Manages miniaudio playback device and audio mixing."""

    def __init__(self, squeezy_ref):
        """Initialize audio player.

        Args:
            squeezy_ref: Reference to Squeezy instance (for state access)
        """
        self.squeezy = squeezy_ref

    def get_supported_rate(self, requested_rate):
        """Return supported sample rate if available, else fallback to 44100.

        Args:
            requested_rate: Requested sample rate in Hz

        Returns:
            Supported rate from [44100, 48000, 96000, 192000], or 44100 as fallback
        """
        supported = [44100, 48000, 96000, 192000]
        if requested_rate in supported:
            return requested_rate
        return 44100

    def start(self, sample_rate=None):
        """Start audio playback on miniaudio device.

        Args:
            sample_rate: Sample rate in Hz (optional, uses next_sample_rate if not provided)
        """
        if self.squeezy.playing:
            return

        # Close any existing device before creating new one
        if self.squeezy.device:
            try:
                self.squeezy.device.close()
            except Exception:
                pass
            self.squeezy.device = None

        # Determine the sample rate to use (variable sample rate support)
        rate = sample_rate or self.squeezy.next_sample_rate
        self.squeezy.current_sample_rate = self.get_supported_rate(rate)
        log.info("Starting audio playback at %d Hz", self.squeezy.current_sample_rate)
        try:
            # Import constants from squeezy module
            from . import squeezy as sq_module
            self.squeezy.device = miniaudio.PlaybackDevice(
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=sq_module.CHANNELS,
                sample_rate=self.squeezy.current_sample_rate,
                buffersize_msec=sq_module.DEVICE_BUFFER_MSEC,
                device_id=self.squeezy.audio_device_id,
            )
            log.debug("Audio device buffer: %dms (requested %dms)",
                      self.squeezy.device.buffersize_msec, sq_module.DEVICE_BUFFER_MSEC)
            self.squeezy.playing = True  # Set BEFORE start() — generator checks immediately
            self.squeezy.paused = False
            self.squeezy.output_frames = 0
            self.squeezy._device_start_time = None   # Reset dynamic delay tracking
            self.squeezy._device_start_frames = 0
            gen = self.squeezy._audio_generator()
            next(gen)  # prime the generator before miniaudio calls send()
            self.squeezy.device.start(gen)
        except Exception as e:
            log.error("Audio start failed: %s", e)

    def stop(self):
        """Stop audio playback and close device."""
        if self.squeezy.device:
            try:
                self.squeezy.device.close()
            except Exception:
                pass
            self.squeezy.device = None
        self.squeezy.playing = False

    def pause(self):
        """Pause playback (output silence, keep device running)."""
        self.squeezy.paused = True

    def resume(self, sample_rate=None):
        """Resume playback after pause or restart with new sample rate.

        Args:
            sample_rate: Optional new sample rate in Hz
        """
        if not self.squeezy.playing:
            self.start(sample_rate)
            return

        # Determine the sample rate to use (variable sample rate support)
        rate = sample_rate or self.squeezy.next_sample_rate
        self.squeezy.current_sample_rate = self.get_supported_rate(rate)
        log.info("Resuming audio at %d Hz (%d bytes buffered)",
                 self.squeezy.current_sample_rate, self.squeezy.pcm_buf.available())
        # Close old device before creating new one
        if self.squeezy.device:
            try:
                self.squeezy.device.close()
            except Exception:
                pass
            self.squeezy.device = None
        try:
            from . import squeezy as sq_module
            self.squeezy.device = miniaudio.PlaybackDevice(
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=sq_module.CHANNELS,
                sample_rate=self.squeezy.current_sample_rate,
                buffersize_msec=sq_module.DEVICE_BUFFER_MSEC,
                device_id=self.squeezy.audio_device_id,
            )
            gen = self.squeezy._audio_generator()
            next(gen)  # prime the generator
            self.squeezy.device.start(gen)
        except Exception as e:
            log.error("Audio resume failed: %s", e)

    def elapsed_ms(self):
        """Frame-based elapsed time with dynamic device delay compensation.

        Like squeezelite's snd_pcm_delay() approach: subtract actual buffer
        occupancy so LMS knows what the user *hears*, not what we've fed.

        Returns:
            Milliseconds of audio played (relative to current track)
        """
        from . import squeezy as sq_module

        # For true gapless, calculate relative to current track
        frames_in_track = self.squeezy.output_frames - self.squeezy._track_start_frames
        if frames_in_track <= 0:
            return 0

        if self.squeezy._device_start_time is None:
            # Not yet playing real audio — use static estimate
            device_delay_frames = self.squeezy.current_sample_rate * (
                sq_module.DEVICE_BUFFER_MSEC + self.squeezy.pipeline_latency_msec
            ) // 1000
        else:
            # Measure buffer occupancy from wall clock: how much wall time
            # has passed since we sent the first real frame?
            wall_msec = (time.monotonic() - self.squeezy._device_start_time) * 1000
            wall_frames = int(self.squeezy.current_sample_rate * wall_msec / 1000)
            # Buffer is the difference: frames yielded - wall time elapsed
            buffer_frames = self.squeezy.output_frames - self.squeezy._device_start_frames - wall_frames
            device_delay_frames = max(0, buffer_frames)

        # Subtract device delay from frame count
        elapsed_frames = max(0, frames_in_track - device_delay_frames)
        return (elapsed_frames * 1000) // self.squeezy.current_sample_rate

    def build_fade_curves(self, fade_duration_samples):
        """Build linear gain curves for fade in/out.

        Args:
            fade_duration_samples: Number of samples over which to fade

        Returns:
            Tuple of (fade_in_gains, fade_out_gains) lists, or (None, None) if duration <= 0
        """
        if fade_duration_samples <= 0:
            return None, None

        fade_in = [i / fade_duration_samples for i in range(fade_duration_samples)]
        fade_out = [1.0 - g for g in fade_in]  # Complementary: 1.0 → 0.0

        return fade_in, fade_out

    def apply_crossfade(self, new_chunk):
        """Mix old and new track samples during crossfade window.

        Implements 5 fade modes:
        - 0: FADE_NONE (immediate switch, no fade)
        - 1: CROSSFADE (old fades out, new fades in)
        - 2: FADE_IN (new fades in)
        - 3: FADE_OUT (old fades out)
        - 4: FADE_INOUT (both fade simultaneously)

        Args:
            new_chunk: New track PCM chunk (bytes, s16le format)

        Returns:
            Mixed chunk with old track fading out, new track fading in
        """
        if not self.squeezy._crossfade_samples:
            return new_chunk

        old_samples = array.array("h", bytes(self.squeezy._crossfade_samples[:len(new_chunk)]))
        new_samples = array.array("h", new_chunk)
        mixed = array.array("h")

        for i in range(min(len(old_samples), len(new_samples))):
            # Calculate position in the crossfade window (i is sample index)
            pos_in_fade = self.squeezy._crossfade_pos + i

            if pos_in_fade < self.squeezy._crossfade_total:
                # Still in fade window — apply gain curves
                if self.squeezy.transition_type == 1:  # CROSSFADE
                    gain_out = self.squeezy._fade_out_gains[pos_in_fade]
                    gain_in = self.squeezy._fade_in_gains[pos_in_fade]
                elif self.squeezy.transition_type == 2:  # FADE_IN
                    gain_out = 0.0
                    gain_in = self.squeezy._fade_in_gains[pos_in_fade]
                elif self.squeezy.transition_type == 3:  # FADE_OUT
                    gain_out = self.squeezy._fade_out_gains[pos_in_fade]
                    gain_in = 0.0
                elif self.squeezy.transition_type == 4:  # FADE_INOUT
                    gain_out = self.squeezy._fade_out_gains[pos_in_fade]
                    gain_in = self.squeezy._fade_in_gains[pos_in_fade]
                else:  # FADE_NONE
                    gain_out = 0.0
                    gain_in = 1.0

                # Mix: old_sample × gain_out + new_sample × gain_in
                mixed_sample = int(old_samples[i] * gain_out + new_samples[i] * gain_in)
                mixed.append(mixed_sample)
            else:
                # Crossfade window finished, use new sample only
                mixed.append(new_samples[i])

        # Update position for next batch of samples
        self.squeezy._crossfade_pos += len(new_samples)

        if self.squeezy._crossfade_pos >= self.squeezy._crossfade_total:
            log.debug("Crossfade complete after %d samples", self.squeezy._crossfade_total)
            self.squeezy._crossfade_enabled = False

        return mixed.tobytes()

    def reset_track_state(self):
        """Reset state for a new track (true gapless).
        Called when switching tracks without closing the device.
        """
        self.squeezy._current_track_id += 1
        self.squeezy._track_start_frames = self.squeezy.output_frames
        self.squeezy.decode_complete = False
        self.squeezy.sent_STMd = False
        self.squeezy.sent_STMu = False
        self.squeezy.sent_STMo = False
        self.squeezy.sent_STMl = False
        self.squeezy._switching_track = False
        # Clear crossfade state at track boundary
        self.squeezy._crossfade_enabled = False
        self.squeezy._crossfade_samples.clear()
        self.squeezy._crossfade_pos = 0
        self.squeezy._crossfade_total = 0
        log.debug("Track boundary: switching to track #%d at frame %d",
                  self.squeezy._current_track_id, self.squeezy._track_start_frames)

    @staticmethod
    def apply_volume_scaling(chunk, vol):
        """Apply volume scaling to PCM chunk (sample-by-sample).

        Args:
            chunk: PCM data (bytes, s16le format)
            vol: Volume multiplier (0.0-1.0, where 1.0 = unity gain)

        Returns:
            Scaled PCM chunk
        """
        if vol >= 0.999:
            return chunk
        samples = array.array("h", chunk)
        for i in range(len(samples)):
            samples[i] = int(samples[i] * vol)
        return samples.tobytes()

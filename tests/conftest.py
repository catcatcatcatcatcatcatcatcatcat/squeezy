"""Pytest fixtures for squeezy integration tests."""

import os
import signal
import subprocess
import sys
import time

import pytest

from tests.lms_client import LMSClient

SQUEEZY_NAME = "squeezy-ci-test"
LMS_HOST = os.environ.get("LMS_HOST", "localhost")
LMS_MUSIC_DIR = os.environ.get("LMS_MUSIC_DIR", "/tmp/lms-music")
# Path as LMS sees it (inside container: /music, or same as host path)
LMS_MUSIC_PATH = os.environ.get("LMS_MUSIC_PATH", "/tmp/lms-music")


@pytest.fixture(scope="session")
def lms():
    """LMS client — waits for server to be ready."""
    client = LMSClient(host=LMS_HOST)
    client.wait_ready(timeout=90)
    return client


@pytest.fixture(scope="session")
def test_tracks(lms):
    """Generate test audio files and configure LMS.

    Creates:
      - test.wav: 30s 440Hz sine (for basic playback tests)
      - sweep.wav: 30s chirp 200Hz→3800Hz (for seek validation)
    """
    os.makedirs(LMS_MUSIC_DIR, exist_ok=True)

    # 30-second constant tone
    tone_path = os.path.join(LMS_MUSIC_DIR, "test.wav")
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=30",
            "-ar", "44100", "-ac", "2", tone_path,
        ],
        check=True, capture_output=True,
    )

    # 30-second frequency sweep (chirp): 200Hz → 3800Hz
    # Instantaneous freq at time t: f(t) = 200 + (3800-200)*t/30 = 200 + 120*t
    # Phase integral: 2*PI*(200*t + 60*t^2)
    sweep_path = os.path.join(LMS_MUSIC_DIR, "sweep.wav")
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "aevalsrc=sin(2*PI*(200*t+60*t*t)):s=44100:d=30",
            "-ac", "2", sweep_path,
        ],
        check=True, capture_output=True,
    )

    # Configure LMS to see these files
    lms.configure_media_dir(LMS_MUSIC_PATH)

    return {
        "tone": os.path.join(LMS_MUSIC_PATH, "test.wav"),
        "sweep": os.path.join(LMS_MUSIC_PATH, "sweep.wav"),
    }


@pytest.fixture
def squeezy_process():
    """Start squeezy as a subprocess, kill on teardown."""
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "squeezy",
            "-s", LMS_HOST,
            "-n", SQUEEZY_NAME,
            "-v",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    time.sleep(2)
    yield proc
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@pytest.fixture
def player_id(squeezy_process, lms):
    """Start squeezy and wait for it to register. Return its player ID."""
    return lms.wait_for_player(SQUEEZY_NAME, timeout=15)

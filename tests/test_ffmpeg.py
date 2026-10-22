"""ffmpeg resolution and failure reporting.

A bare "ffmpeg" works from a shell but not under launchd/systemd, and the
failure was silent: the codec probe still advertised FLAC & co, so LMS streamed
formats the player couldn't decode.
"""

import logging
from unittest.mock import Mock, patch

import pytest

from squeezy import Squeezy
from squeezy import squeezy as squeezy_mod


def _make_ffmpeg(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "ffmpeg"
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def fake_ffmpeg(tmp_path):
    return _make_ffmpeg(tmp_path / "bin")


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("SQUEEZY_FFMPEG", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))


class TestResolveFfmpeg:
    def test_override_wins(self, fake_ffmpeg, monkeypatch):
        monkeypatch.setenv("SQUEEZY_FFMPEG", "/nonexistent/ffmpeg")
        assert squeezy_mod.resolve_ffmpeg(fake_ffmpeg) == fake_ffmpeg

    def test_env_beats_path(self, fake_ffmpeg, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path / "other"))
        _make_ffmpeg(tmp_path / "other")
        monkeypatch.setenv("SQUEEZY_FFMPEG", fake_ffmpeg)
        assert squeezy_mod.resolve_ffmpeg() == fake_ffmpeg

    def test_found_on_path(self, fake_ffmpeg, clean_env, monkeypatch):
        monkeypatch.setenv("PATH", squeezy_mod.os.path.dirname(fake_ffmpeg))
        assert squeezy_mod.resolve_ffmpeg() == fake_ffmpeg

    def test_bare_name_override_is_looked_up_on_path(self, fake_ffmpeg, clean_env, monkeypatch):
        monkeypatch.setenv("PATH", squeezy_mod.os.path.dirname(fake_ffmpeg))
        monkeypatch.setenv("SQUEEZY_FFMPEG", "ffmpeg")
        assert squeezy_mod.resolve_ffmpeg() == fake_ffmpeg

    def test_tilde_is_expanded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        path = _make_ffmpeg(tmp_path / "bin")
        assert squeezy_mod.resolve_ffmpeg("~/bin/ffmpeg") == path

    def test_launchd_path_without_homebrew(self, clean_env):
        assert squeezy_mod.resolve_ffmpeg() is None

    def test_missing_override(self, clean_env):
        assert squeezy_mod.resolve_ffmpeg("/nonexistent/ffmpeg") is None

    def test_not_executable(self, fake_ffmpeg):
        squeezy_mod.os.chmod(fake_ffmpeg, 0o644)
        assert squeezy_mod.resolve_ffmpeg(fake_ffmpeg) is None

    def test_squeezy_uses_given_path(self, fake_ffmpeg):
        assert Squeezy(name="test", ffmpeg=fake_ffmpeg).ffmpeg == fake_ffmpeg


class TestCli:
    def test_missing_ffmpeg_exits_with_actionable_error(self, clean_env, monkeypatch, caplog):
        monkeypatch.setattr(squeezy_mod.sys, "argv", ["squeezy"])
        with patch.object(squeezy_mod, "check_for_update"), \
                patch.object(squeezy_mod, "Squeezy") as player, \
                pytest.raises(SystemExit) as exit_info:
            squeezy_mod.main()
        assert exit_info.value.code == 1
        assert "--ffmpeg" in caplog.text and "SQUEEZY_FFMPEG" in caplog.text
        player.assert_not_called()

    def test_ffmpeg_flag_reaches_player(self, fake_ffmpeg, clean_env, monkeypatch):
        monkeypatch.setattr(squeezy_mod.sys, "argv", ["squeezy", "--ffmpeg", fake_ffmpeg])
        with patch.object(squeezy_mod, "check_for_update"), \
                patch.object(squeezy_mod, "Squeezy") as player, \
                patch.object(squeezy_mod.signal, "signal"):
            squeezy_mod.main()
        assert player.call_args.kwargs["ffmpeg"] == fake_ffmpeg


class TestProbeCodecs:
    def test_missing_ffmpeg_advertises_pcm_only(self):
        with patch.object(squeezy_mod.subprocess, "run", side_effect=FileNotFoundError):
            assert Squeezy._probe_ffmpeg_codecs("/nope/ffmpeg") == ["pcm"]

    def test_nonzero_exit_falls_back_to_standard(self):
        result = Mock(returncode=1, stdout="")
        with patch.object(squeezy_mod.subprocess, "run", return_value=result):
            assert "flac" in Squeezy._probe_ffmpeg_codecs("/usr/bin/ffmpeg")

    def test_probe_uses_resolved_path(self):
        result = Mock(returncode=0, stdout=" A....D flac  FLAC\n")
        with patch.object(squeezy_mod.subprocess, "run", return_value=result) as run:
            Squeezy._probe_ffmpeg_codecs("/opt/homebrew/bin/ffmpeg")
        assert run.call_args[0][0][0] == "/opt/homebrew/bin/ffmpeg"


class TestDecoderLaunch:
    def test_launch_failure_is_reported_as_decode_error(self, caplog):
        player = Squeezy(name="test", ffmpeg="/nope/ffmpeg")
        player._send = Mock()
        player._send_stat = Mock()
        player.streaming = True

        sock = Mock()
        sock.recv.side_effect = [b"HTTP/1.0 200 OK\r\n\r\n", b""]
        with patch.object(squeezy_mod.socket, "socket", return_value=sock), \
                patch.object(squeezy_mod.subprocess, "Popen",
                             side_effect=FileNotFoundError(2, "No such file")) as popen, \
                caplog.at_level(logging.ERROR, logger="squeezy"):
            player._stream_worker("10.0.0.1", 9000, b"GET / HTTP/1.0\r\n\r\n",
                                  threshold=0, autostart=1, fmt="f")

        assert popen.call_args[0][0][0] == "/nope/ffmpeg"
        assert "Cannot start ffmpeg" in caplog.text
        assert [c.args[0] for c in player._send_stat.call_args_list] == ["STMc", "STMn"]
        sock.close.assert_called()
        assert player.stream_sock is None

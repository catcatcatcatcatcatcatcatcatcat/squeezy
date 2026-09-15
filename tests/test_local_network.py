"""Unit tests for the macOS Local Network privacy helpers.

macOS 15+ gates LAN traffic per process. A denied process gets EHOSTUNREACH
("No route to host") on connect()/sendto() to any LAN address, which looks
exactly like a routing problem — so squeezy explains it and offers a way to
trigger the permission prompt.
"""

import errno
import logging
import os
import socket
import sys
import tempfile
from unittest.mock import Mock, patch

import pytest

from squeezy import Squeezy
from squeezy.network import local_network
from squeezy.network.server_connection import ServerConnection
from squeezy.protocol import slimproto


def _unreachable():
    return OSError(errno.EHOSTUNREACH, "No route to host")


class TestHintFor:
    """hint_for() recognises a macOS Local Network denial and nothing else."""

    def test_ehostunreach_to_lan_address_on_macos_gives_hint(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        hint = local_network.hint_for(_unreachable(), "10.10.10.26")
        assert hint is not None
        assert "Local Network" in hint
        assert "--request-local-network" in hint

    def test_other_errno_gives_no_hint(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        err = OSError(errno.ECONNREFUSED, "Connection refused")
        assert local_network.hint_for(err, "10.10.10.26") is None

    def test_public_address_gives_no_hint(self, monkeypatch):
        """A genuine routing failure to the internet is not a permission problem."""
        monkeypatch.setattr(sys, "platform", "darwin")
        assert local_network.hint_for(_unreachable(), "1.1.1.1") is None

    def test_off_macos_gives_no_hint(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert local_network.hint_for(_unreachable(), "10.10.10.26") is None

    def test_hostname_gives_hint(self, monkeypatch):
        """-s lms.local: can't classify a name, and a LAN name is the common case."""
        monkeypatch.setattr(sys, "platform", "darwin")
        assert local_network.hint_for(_unreachable(), "lms.local") is not None

    def test_broadcast_address_gives_hint(self, monkeypatch):
        """Discovery broadcasts are LAN traffic too."""
        monkeypatch.setattr(sys, "platform", "darwin")
        assert local_network.hint_for(_unreachable(), "255.255.255.255") is not None


class TestConnectLogsHint:
    """The hint follows the 'Connection failed' error where users actually see it."""

    def test_connect_logs_hint_on_ehostunreach(self, monkeypatch, caplog):
        monkeypatch.setattr(sys, "platform", "darwin")
        sock = Mock()
        sock.connect.side_effect = _unreachable()
        player = Squeezy(name="test", server="10.10.10.26")
        with patch("socket.socket", return_value=sock), caplog.at_level(logging.ERROR, logger="squeezy"):
            assert player.connect() is False
        messages = [r.getMessage() for r in caplog.records]
        assert any("No route to host" in m for m in messages)
        assert any("Local Network" in m for m in messages)

    def test_connect_logs_hint_only_once_per_run(self, monkeypatch, caplog):
        """The reconnect loop retries every few seconds; don't repeat the essay."""
        monkeypatch.setattr(sys, "platform", "darwin")
        sock = Mock()
        sock.connect.side_effect = _unreachable()
        player = Squeezy(name="test", server="10.10.10.26")
        with patch("socket.socket", return_value=sock), caplog.at_level(logging.ERROR, logger="squeezy"):
            assert player.connect() is False
            assert player.connect() is False
        messages = [r.getMessage() for r in caplog.records]
        assert sum("Connection failed" in m for m in messages) == 2
        assert sum("Local Network" in m for m in messages) == 1

    def test_connect_logs_no_hint_for_refused(self, monkeypatch, caplog):
        monkeypatch.setattr(sys, "platform", "darwin")
        sock = Mock()
        sock.connect.side_effect = OSError(errno.ECONNREFUSED, "Connection refused")
        player = Squeezy(name="test", server="10.10.10.26")
        with patch("socket.socket", return_value=sock), caplog.at_level(logging.ERROR, logger="squeezy"):
            assert player.connect() is False
        assert not any("Local Network" in r.getMessage() for r in caplog.records)


class TestDiscoveryLogsHint:
    """A denied broadcast otherwise degrades silently into 'No server found'."""

    def _denied_udp_socket(self):
        sock = Mock()
        sock.sendto.side_effect = _unreachable()
        sock.recvfrom.side_effect = socket.timeout()
        sock.getsockname.return_value = ("10.10.10.220", 0)
        return sock

    def test_discovery_logs_hint_when_every_broadcast_is_refused(self, monkeypatch, caplog):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(slimproto, "DISCOVERY_ATTEMPTS", 1)
        udp = self._denied_udp_socket()
        tcp = Mock()
        tcp.connect.side_effect = OSError(errno.ECONNREFUSED, "Connection refused")

        def fake_socket(family, kind, *a, **kw):
            return udp if kind == socket.SOCK_DGRAM else tcp

        with patch("socket.socket", side_effect=fake_socket), caplog.at_level(logging.WARNING, logger="squeezy"):
            assert ServerConnection.discover_lms() is None
        hints = [r for r in caplog.records if "Local Network" in r.getMessage()]
        assert len(hints) == 1, "hint should be logged exactly once"

    def test_discovery_logs_no_hint_when_broadcast_merely_unanswered(self, monkeypatch, caplog):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(slimproto, "DISCOVERY_ATTEMPTS", 1)
        udp = self._denied_udp_socket()
        udp.sendto.side_effect = None  # sends fine, nobody answers
        tcp = Mock()
        tcp.connect.side_effect = OSError(errno.ECONNREFUSED, "Connection refused")

        def fake_socket(family, kind, *a, **kw):
            return udp if kind == socket.SOCK_DGRAM else tcp

        with patch("socket.socket", side_effect=fake_socket), caplog.at_level(logging.WARNING, logger="squeezy"):
            assert ServerConnection.discover_lms() is None
        assert not any("Local Network" in r.getMessage() for r in caplog.records)


class TestProbe:
    """probe() is what the re-spawned child runs: 0 allowed, 1 denied, 2 unknown."""

    def test_probe_reports_allowed_when_server_connects(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            assert local_network.probe("127.0.0.1", port=port) == local_network.PROBE_ALLOWED
        finally:
            listener.close()

    def test_probe_reports_denied_on_ehostunreach(self):
        sock = Mock()
        sock.connect.side_effect = _unreachable()
        with patch("socket.socket", return_value=sock):
            assert local_network.probe("10.10.10.26") == local_network.PROBE_DENIED

    def test_probe_reports_unknown_when_nothing_found(self):
        """Refused/no answer says nothing about the permission."""
        sock = Mock()
        sock.connect.side_effect = OSError(errno.ECONNREFUSED, "Connection refused")
        with patch("socket.socket", return_value=sock):
            assert local_network.probe("10.10.10.26") == local_network.PROBE_UNKNOWN

    def test_probe_without_server_uses_discovery(self):
        with patch.object(ServerConnection, "discover_lms", return_value="10.10.10.26") as discover:
            assert local_network.probe(None) == local_network.PROBE_ALLOWED
        discover.assert_called_once()

    def test_probe_without_server_and_no_answer_is_unknown(self):
        with patch.object(ServerConnection, "discover_lms", return_value=None):
            assert local_network.probe(None) == local_network.PROBE_UNKNOWN


@pytest.mark.skipif(sys.platform != "darwin", reason="responsibility_spawnattrs_setdisclaim is macOS-only")
class TestSpawnDisclaimed:
    def test_returns_child_exit_code(self):
        assert local_network.spawn_disclaimed([sys.executable, "-c", "import sys; sys.exit(7)"]) == 7

    def test_child_is_its_own_responsible_process(self):
        """The whole point: macOS must attribute the child's LAN access to Python itself."""
        with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False) as f:
            out = f.name
        try:
            code = (
                "import ctypes, os\n"
                "q = ctypes.CDLL('/usr/lib/system/libquarantine.dylib')\n"
                "q.responsibility_get_pid_responsible_for_pid.restype = ctypes.c_int\n"
                f"open({out!r}, 'w').write(str(q.responsibility_get_pid_responsible_for_pid(os.getpid()) == os.getpid()))\n"
            )
            assert local_network.spawn_disclaimed([sys.executable, "-c", code]) == 0
            with open(out) as f:
                assert f.read() == "True"
        finally:
            os.unlink(out)

    def test_child_inherits_environment(self):
        with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False) as f:
            out = f.name
        try:
            code = f"import os; open({out!r}, 'w').write(os.environ.get('SQUEEZY_TEST_ENV', ''))"
            with patch.dict(os.environ, {"SQUEEZY_TEST_ENV": "inherited"}):
                assert local_network.spawn_disclaimed([sys.executable, "-c", code]) == 0
            with open(out) as f:
                assert f.read() == "inherited"
        finally:
            os.unlink(out)


class TestRequestAccess:
    """request_access() re-runs squeezy's probe as its own responsible process and explains the result."""

    def test_spawns_probe_with_server_and_reports_allowed(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "darwin")
        with patch.object(local_network, "spawn_disclaimed", return_value=local_network.PROBE_ALLOWED) as spawn:
            assert local_network.request_access("10.10.10.26") == 0
        argv = spawn.call_args.args[0]
        assert argv[0] == sys.executable
        assert "--local-network-probe" in argv
        assert argv[argv.index("-s") + 1] == "10.10.10.26"
        assert "allowed" in capsys.readouterr().out.lower()

    def test_reports_denied_with_instructions(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "darwin")
        with patch.object(local_network, "spawn_disclaimed", return_value=local_network.PROBE_DENIED):
            assert local_network.request_access(None) != 0
        out = capsys.readouterr().out
        assert "Allow" in out
        assert "Privacy & Security" in out

    def test_off_macos_is_a_noop(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "linux")
        with patch.object(local_network, "spawn_disclaimed") as spawn:
            assert local_network.request_access(None) == 0
        spawn.assert_not_called()
        assert "macOS" in capsys.readouterr().out


class TestCli:
    def test_request_local_network_flag_runs_request_and_exits(self, monkeypatch):
        from squeezy import squeezy as squeezy_main
        monkeypatch.setattr(sys, "argv", ["squeezy", "-s", "10.10.10.26", "--request-local-network"])
        with patch.object(local_network, "request_access", return_value=3) as req, \
                patch.object(squeezy_main, "Squeezy") as player:
            with pytest.raises(SystemExit) as exc:
                squeezy_main.main()
        assert exc.value.code == 3
        req.assert_called_once_with("10.10.10.26")
        player.assert_not_called()

    def test_local_network_probe_flag_runs_probe_and_exits(self, monkeypatch):
        from squeezy import squeezy as squeezy_main
        monkeypatch.setattr(sys, "argv", ["squeezy", "--local-network-probe"])
        with patch.object(local_network, "probe", return_value=local_network.PROBE_DENIED) as probe, \
                patch.object(squeezy_main, "Squeezy") as player:
            with pytest.raises(SystemExit) as exc:
                squeezy_main.main()
        assert exc.value.code == local_network.PROBE_DENIED
        probe.assert_called_once_with(None)
        player.assert_not_called()

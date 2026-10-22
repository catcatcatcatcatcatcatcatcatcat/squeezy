"""Player MAC (LMS player ID) detection.

uuid.getnode() returns the placeholder 02:00:00:00:00:00 on macOS 26+, so every
Mac claimed the same player ID and LMS evicted one player for the other.
"""

from unittest.mock import patch

import pytest

from squeezy import Squeezy
from squeezy.config import config
from squeezy.protocol import slimproto


PLACEHOLDER = 0x020000000000
RANDOM_MULTICAST = 0x0F1E2D3C4B5A         # what getnode() falls back to

NETWORKSETUP = """\
Hardware Port: Thunderbolt Bridge
Device: bridge0
Ethernet Address: 36:46:4e:09:47:80

Hardware Port: Wi-Fi
Device: en0
Ethernet Address: 60:3e:5f:61:7c:c5

VLAN Configurations
===================
"""

IFCONFIG = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
bridge0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 36:46:4e:09:47:80
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 3e:d0:33:c8:8a:93
"""


@pytest.fixture
def no_sysfs(monkeypatch, tmp_path):
    monkeypatch.setattr(slimproto, "SYSFS_NET", str(tmp_path / "absent"))


@pytest.fixture
def fake_sysfs(monkeypatch, tmp_path):
    root = tmp_path / "net"

    def add(name, address):
        (root / name).mkdir(parents=True)
        (root / name / "address").write_text(address + "\n")

    monkeypatch.setattr(slimproto, "SYSFS_NET", str(root))
    return add


def _fake_run(outputs):
    return lambda argv: outputs.get(argv[0], "")


def _getnode(value):
    return patch.object(slimproto.uuid, "getnode", return_value=value)


class TestMacPredicates:
    def test_rejects_macos_getnode_placeholder(self):
        assert not slimproto.is_usable_mac(b"\x02\x00\x00\x00\x00\x00")

    def test_rejects_zero_and_multicast(self):
        assert not slimproto.is_usable_mac(b"\x00" * 6)
        assert not slimproto.is_usable_mac(b"\x01\x23\x45\x67\x89\xab")

    def test_rejects_wrong_length(self):
        assert not slimproto.is_usable_mac(b"\x14\x98\x77\x38")

    def test_accepts_real_nic_as_global(self):
        assert slimproto.is_global_mac(b"\x14\x98\x77\x38\xb7\x45")

    def test_locally_administered_is_usable_but_not_global(self):
        docker_eth0 = b"\x02\x42\xac\x11\x00\x02"
        assert slimproto.is_usable_mac(docker_eth0)
        assert not slimproto.is_global_mac(docker_eth0)


class TestDefaultMac:
    def test_never_returns_a_placeholder(self):
        mac = slimproto.default_mac()
        assert mac is None or slimproto.is_usable_mac(mac)

    def test_usable_getnode_is_kept(self):
        """Existing player IDs (Linux, Windows, macOS < 26) must not change."""
        with _getnode(0x149877_38B745), \
                patch.object(slimproto, "_interface_macs") as scan:
            assert slimproto.default_mac() == bytes.fromhex("14987738b745")
        scan.assert_not_called()

    def test_locally_administered_getnode_is_kept(self):
        """e.g. a container's eth0 — what earlier releases used, so keep it."""
        with _getnode(0x0242AC_110002):
            assert slimproto.default_mac() == bytes.fromhex("0242ac110002")

    @pytest.mark.parametrize("node", [PLACEHOLDER, RANDOM_MULTICAST])
    def test_unusable_getnode_falls_back_to_interfaces(self, node):
        found = [("bridge0", "36:46:4e:09:47:80"), ("en0", "60:3e:5f:61:7c:c5")]
        with _getnode(node), patch.object(slimproto, "_interface_macs", return_value=found):
            assert slimproto.default_mac() == bytes.fromhex("603e5f617cc5")

    def test_prefers_en0_over_earlier_global(self):
        found = [("en7", "14:98:77:38:b7:45"), ("en0", "60:3e:5f:61:7c:c5")]
        with _getnode(PLACEHOLDER), patch.object(slimproto, "_interface_macs", return_value=found):
            assert slimproto.default_mac() == bytes.fromhex("603e5f617cc5")

    def test_locally_administered_interfaces_are_not_used(self):
        """Docker's 02:42:* repeats across hosts; random Wi-Fi rotates."""
        found = [("eth0", "02:42:ac:11:00:02"), ("wlan0", "3e:d0:33:c8:8a:93")]
        with _getnode(PLACEHOLDER), patch.object(slimproto, "_interface_macs", return_value=found):
            assert slimproto.default_mac() is None

    def test_none_when_nothing_usable(self):
        found = [("lo", "00:00:00:00:00:00"), ("x", "garbage")]
        with _getnode(PLACEHOLDER), patch.object(slimproto, "_interface_macs", return_value=found):
            assert slimproto.default_mac() is None


class TestInterfaceScan:
    def test_linux_sysfs(self, fake_sysfs):
        fake_sysfs("lo", "00:00:00:00:00:00")
        fake_sysfs("docker0", "02:42:5c:1a:2b:3c")
        fake_sysfs("tunl0", "00:00:00:00")            # ipip: 4-byte address
        fake_sysfs("enp3s0", "14:98:77:38:b7:45")
        with _getnode(RANDOM_MULTICAST):
            assert slimproto.default_mac() == bytes.fromhex("14987738b745")

    def test_macos_uses_hardware_address_not_private_wifi(self, no_sysfs, monkeypatch):
        """ifconfig shows en0's Private Wi-Fi address; networksetup the real one."""
        monkeypatch.setattr(slimproto.sys, "platform", "darwin")
        outputs = {"/usr/sbin/networksetup": NETWORKSETUP, "/sbin/ifconfig": IFCONFIG}
        with _getnode(PLACEHOLDER), \
                patch.object(slimproto, "_run", side_effect=_fake_run(outputs)):
            assert slimproto.default_mac() == bytes.fromhex("603e5f617cc5")

    def test_ifconfig_fallback_when_networksetup_missing(self, no_sysfs, monkeypatch):
        monkeypatch.setattr(slimproto.sys, "platform", "darwin")
        outputs = {"/sbin/ifconfig": IFCONFIG}
        with patch.object(slimproto, "_run", side_effect=_fake_run(outputs)):
            assert slimproto._interface_macs() == [
                ("bridge0", "36:46:4e:09:47:80"),
                ("en0", "3e:d0:33:c8:8a:93"),
            ]

    def test_missing_tools_yield_nothing(self, no_sysfs, monkeypatch):
        monkeypatch.setattr(slimproto.sys, "platform", "win32")
        with patch.object(slimproto.subprocess, "run", side_effect=FileNotFoundError):
            assert slimproto._interface_macs() == []

    def test_undecodable_output_does_not_raise(self):
        out = slimproto._run(["/usr/bin/printf", "Device: \\377\\n"])
        assert out.startswith("Device: ")


class TestPersistedFallbackMac:
    def test_generated_once_then_reused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        first = config.load_or_create_mac()
        assert slimproto.is_usable_mac(first)
        assert first[0] & 0x02                     # locally administered
        assert config.load_or_create_mac() == first
        assert list((tmp_path / "squeezy").iterdir()) == [tmp_path / "squeezy" / "mac"]

    def test_corrupt_file_is_replaced(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        (tmp_path / "squeezy").mkdir()
        (tmp_path / "squeezy" / config.MAC_FILE).write_text("not a mac")
        mac = config.load_or_create_mac()
        assert slimproto.is_usable_mac(mac)
        assert config.load_or_create_mac() == mac

    def test_unwritable_config_dir_still_returns_a_mac(self, monkeypatch, caplog):
        def no_config_dir():
            raise PermissionError(13, "Read-only file system")
        monkeypatch.setattr(config, "get_config_dir", no_config_dir)
        mac = config.load_or_create_mac()
        assert slimproto.is_usable_mac(mac)
        assert "will change on restart" in caplog.text


class TestSqueezyMac:
    def test_explicit_mac_wins(self):
        player = Squeezy(name="test", mac="aa:bb:cc:dd:ee:ff")
        assert player.mac == bytes.fromhex("aabbccddeeff")

    def test_no_interface_uses_persisted_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        with patch.object(slimproto, "default_mac", return_value=None):
            a = Squeezy(name="a")
            b = Squeezy(name="b")
        assert a.mac == b.mac
        assert slimproto.is_usable_mac(a.mac)

    def test_macos_26_placeholder_is_never_the_player_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        with _getnode(PLACEHOLDER):
            mac = Squeezy(name="test").mac
        assert mac != b"\x02\x00\x00\x00\x00\x00"
        assert slimproto.is_usable_mac(mac)

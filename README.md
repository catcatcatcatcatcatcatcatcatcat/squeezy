# Squeezy

Minimal Squeezebox-compatible player for [Lyrion Music Server](https://lyrion.org/) (formerly Logitech Media Server). Advertises as a player on your network, receives streaming audio, and plays it back through your default audio output. Supports synchronized playback with other players.

## Before you use this

**If you can run a binary, use [LocalPlayer](https://github.com/LMS-Community/plugin-LocalPlayer) instead.** It's a Lyrion Music Server plugin that bundles [squeezelite](https://github.com/ralph-irving/squeezelite) — a proper, mature, battle-tested C implementation. It installs in two clicks from the LMS plugin browser, handles every edge case, and just works. Nearly every protocol detail and edge case in squeezy was reverse-engineered from squeezelite's source. It is the gold standard and the direct inspiration for this project.

**Squeezy exists for one narrow situation:** you're on a machine where you can't run an unverified or unpackaged binary — a locked-down work laptop, a corporate machine with strict security policy, a CI environment — but you *can* run Python packages. That's how this project started: wanting to listen to music on a work laptop through Lyrion without having to file an IT ticket to run a compiled binary from the internet. `pip install squeezy` and it just works.

If that's not your situation, seriously — go use LocalPlayer. It's better in every way.

---

> **Note:** This project was largely vibe-coded with [Claude](https://claude.ai). It passes ~100 tests and works reliably day-to-day, but treat it accordingly.

---

## Requirements

- macOS, Linux, or Windows
- Python 3.9+
- [ffmpeg](https://ffmpeg.org/)

## Install

Pick whichever method suits you. All three result in a `squeezy` command on your PATH.

### Option 1: pipx (recommended)

```bash
# macOS
brew install ffmpeg pipx
pipx install squeezy

# Debian/Ubuntu
sudo apt install ffmpeg pipx
pipx install squeezy
```

### Option 2: Homebrew tap (macOS only)

```bash
brew tap catcatcatcatcatcatcatcatcatcat/tap
brew install squeezy
```

This installs ffmpeg automatically as a dependency.

### Option 3: From source

```bash
git clone https://github.com/catcatcatcatcatcatcatcatcatcat/squeezy.git
cd squeezy
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

For development (run directly without installing):

```bash
./run.sh -n "My Speaker" -vv
```

Squeezy checks for updates on startup and will notify you when a new version is available.

## Usage

```bash
# Auto-discover server on local network
squeezy

# Specify server and player name
squeezy -s 192.168.1.100 -n "Kitchen Speaker"

# Custom MAC address (for persistent player identity)
squeezy -m aa:bb:cc:dd:ee:ff

# List available audio output devices
squeezy -l

# Use a specific audio output (substring match, case-insensitive)
squeezy -d "HDMI" -n "Living Room"

# Verbose logging (connection, playback events, volume changes)
squeezy -v

# Debug logging (protocol-level detail: strm commands, STAT packets)
squeezy -vv

# Check installed version
squeezy --version
```

Your player will appear in the Lyrion Music Server web UI. Select it from the player dropdown to start streaming.

## Project Structure

```
src/squeezy/
├── squeezy.py              # Main player orchestrator & audio pipeline
├── audio/
│   └── stream_decoder.py   # Thread-safe PCMBuffer
├── protocol/
│   ├── handler.py          # SlimProto message handlers (strm, audg, setd, etc.)
│   ├── slimproto.py        # Protocol constants & packet builders
│   └── lms_client.py       # LMS message operations
├── network/
│   ├── server_connection.py # TCP/UDP socket management & discovery
│   ├── lms_metadata.py     # LMS JSON-RPC track metadata queries
│   └── status_server.py    # Unix socket status server
└── config/
    ├── config.py           # XDG-compliant config persistence
    └── metadata.py         # ICY metadata & LAME gapless parsing
```

## Contributing

See [DEVELOPER.md](DEVELOPER.md) for a full walkthrough of the repo structure,
how to set up a dev environment, and how to cut a release.

## How it works

Squeezy implements the [SlimProto protocol](https://wiki.slimdevices.com/index.php/SlimProto_TCP_protocol) to communicate with Lyrion Music Server:

1. Discovers the server via UDP broadcast on port 3483
2. Registers as a player via TCP (HELO packet)
3. Receives stream commands from the server
4. Fetches audio via HTTP, decodes with ffmpeg, outputs via [miniaudio](https://github.com/irmen/pyminiaudio)
5. Reports playback status back to the server for sync coordination

## Uninstall

```bash
# If installed via pipx
pipx uninstall squeezy

# If installed via Homebrew
brew uninstall squeezy
brew untap catcatcatcatcatcatcatcatcatcat/tap
```

## Releasing (for maintainers)

See [DEVELOPER.md](DEVELOPER.md#how-to-release-a-new-version) for the full
release process. The short version: bump `version` in `pyproject.toml`, tag,
build, upload to PyPI, update the Homebrew tap.

## License

MIT

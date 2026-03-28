# Squeezy

Minimal Squeezebox-compatible player for [Lyrion Music Server](https://lyrion.org/) (formerly Logitech Media Server). Advertises as a player on your network, receives streaming audio, and plays it back through your default audio output. Supports synchronized playback with other players.

## Requirements

- macOS (other platforms untested)
- Python 3.10+
- [ffmpeg](https://ffmpeg.org/)

## Install

Pick whichever method suits you. All three result in a `squeezy` command on your PATH.

### Option 1: pip/pipx (recommended)

```bash
brew install ffmpeg pipx
pipx install squeezy
```

### Option 2: Homebrew tap

```bash
brew tap catcatcatcatcatcatcatcatcatcat/tap
brew install squeezy
```

This installs ffmpeg automatically as a dependency.

### Option 3: From source

```bash
brew install ffmpeg
git clone https://github.com/catcatcatcatcatcatcatcatcatcat/squeezy.git
cd squeezy
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

## Usage

```bash
# Auto-discover server on local network
squeezy

# Specify server and player name
squeezy -s 192.168.1.100 -n "Kitchen Speaker"

# Custom MAC address (for persistent player identity)
squeezy -m aa:bb:cc:dd:ee:ff

# Debug logging
squeezy -v
```

Your player will appear in the Lyrion Music Server web UI. Select it from the player dropdown to start streaming.

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

## License

MIT

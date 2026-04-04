# Squeezy

Minimal Squeezebox-compatible player for [Lyrion Music Server](https://lyrion.org/) (formerly Logitech Media Server). Advertises as a player on your network, receives streaming audio, and plays it back through your default audio output. Supports synchronized playback with other players.

## Requirements

- macOS, Linux, or Windows
- Python 3.10+
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

Version is defined in one place: `pyproject.toml`. To release:

```bash
# 1. Bump version in pyproject.toml
# 2. Commit, tag, and push
git add -A && git commit -m "Release vX.Y.Z"
git tag vX.Y.Z && git push origin main vX.Y.Z

# 3. Build and publish to PyPI
python -m build && twine upload dist/squeezy-X.Y.Z*

# 4. Update Homebrew tap (macOS formula)
# Get the SHA256 of the new tag tarball:
curl -sL https://github.com/catcatcatcatcatcatcatcatcatcat/squeezy/archive/refs/tags/vX.Y.Z.tar.gz | shasum -a 256
# Update url + sha256 in homebrew-tap/Formula/squeezy.rb, commit and push
```

All installed users will see an upgrade notice on next startup.

## License

MIT

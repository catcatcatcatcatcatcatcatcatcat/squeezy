# Squeezy

Minimal Squeezebox-compatible player for [Lyrion Music Server](https://lyrion.org/) (formerly Logitech Media Server). Advertises as a player on your network, receives streaming audio, and plays it back through your default audio output. Supports synchronized playback with other players.

## Requirements

- Python 3.10+
- ffmpeg

## Install

### macOS (recommended)

```bash
brew install ffmpeg pipx
pipx install git+https://github.com/catcatcatcatcatcatcatcatcatcat/squeezy.git
```

Then just run:

```bash
squeezy -n "Kitchen Speaker"
```

### From source

```bash
brew install ffmpeg
git clone https://github.com/catcatcatcatcatcatcatcatcatcat/squeezy.git
cd squeezy
make install
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

## How it works

Squeezy implements the SlimProto protocol to communicate with Lyrion Music Server:

1. Discovers the server via UDP broadcast on port 3483
2. Registers as a player via TCP (HELO packet)
3. Receives stream commands from the server
4. Fetches audio via HTTP, decodes with ffmpeg, outputs via miniaudio
5. Reports playback status back to the server for sync coordination

## License

MIT

#!/bin/bash
# Test squeezy install + playback on a Linux distro.
# Expects: LMS reachable at 127.0.0.1:3483, music files at /music in LMS container.
# Usage: run inside a container with --network container:squeezy-lms
set -e

DISTRO=$(cat /etc/os-release | grep ^PRETTY_NAME | cut -d'"' -f2)
echo "=== Testing on: $DISTRO ==="

# Step 1: Install system deps
echo "[1/5] Installing system dependencies..."
if command -v apt-get &>/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq pulseaudio python3-pip python3-venv > /dev/null 2>&1
    apt-get install -y -qq ffmpeg > /dev/null 2>&1 || true
    apt-get install -y -qq pipx > /dev/null 2>&1 || true
elif command -v dnf &>/dev/null; then
    dnf install -y -q ffmpeg pipx pulseaudio python3-pip > /dev/null 2>&1
elif command -v apk &>/dev/null; then
    apk add --quiet ffmpeg pipx pulseaudio py3-pip python3-dev gcc musl-dev > /dev/null 2>&1
fi

# Step 2: Set up PulseAudio null sink
echo "[2/5] Setting up audio..."
pulseaudio --start --exit-idle-time=-1 2>/dev/null || true
pactl load-module module-null-sink sink_name=test_sink 2>/dev/null || true
export PULSE_SINK=test_sink

# Ensure pip-installed scripts are on PATH
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

# Step 3: Install squeezy from local source (copy to writable dir for egg-info)
echo "[3/5] Installing squeezy..."
cp -r /app /tmp/squeezy-src
pip install --break-system-packages -q /tmp/squeezy-src 2>/dev/null \
  || pip3 install --break-system-packages -q /tmp/squeezy-src 2>/dev/null \
  || pip install -q /tmp/squeezy-src 2>/dev/null \
  || pip3 install -q /tmp/squeezy-src 2>/dev/null
echo "    squeezy $(squeezy --version 2>&1)"

# Step 4: Verify help works
echo "[4/5] Verifying --help..."
squeezy --help > /dev/null
echo "    --help OK"

# Step 5: Connect to LMS and test playback
echo "[5/5] Testing playback against LMS..."
squeezy -s 127.0.0.1 -n "test-$(hostname)" -vv &
SQUEEZY_PID=$!
sleep 5

# Find our player
PLAYER_ID=$(python3 -c "
import json, urllib.request
resp = urllib.request.urlopen('http://127.0.0.1:9000/jsonrpc.js',
    json.dumps({'id':1,'method':'slim.request','params':['',['players','0','100']]}).encode())
players = json.loads(resp.read())['result'].get('players_loop', [])
matches = [p['playerid'] for p in players if p['name'].startswith('test-')]
print(matches[0] if matches else '')
" 2>/dev/null)

if [ -z "$PLAYER_ID" ]; then
    echo "    FAIL: Player not registered in LMS"
    kill $SQUEEZY_PID 2>/dev/null
    exit 1
fi
echo "    Player registered: $PLAYER_ID"

# Play a track
python3 -c "
import json, urllib.request, time
def rpc(params):
    data = json.dumps({'id':1,'method':'slim.request','params':params}).encode()
    return json.loads(urllib.request.urlopen('http://127.0.0.1:9000/jsonrpc.js', data).read())
rpc(['$PLAYER_ID', ['playlist', 'play', 'file:///music/test.wav']])
time.sleep(6)
status = rpc(['$PLAYER_ID', ['status', '0', '100']])['result']
mode = status.get('mode', 'unknown')
elapsed = float(status.get('time', 0))
print(f'    mode={mode} elapsed={elapsed:.1f}s')
if mode != 'play':
    print('    FAIL: Expected mode=play')
    exit(1)
if elapsed < 1.0:
    print('    FAIL: Elapsed time not advancing')
    exit(1)
print('    Playback OK')
"
RESULT=$?

kill $SQUEEZY_PID 2>/dev/null
wait $SQUEEZY_PID 2>/dev/null

if [ $RESULT -eq 0 ]; then
    echo "=== PASS: $DISTRO ==="
else
    echo "=== FAIL: $DISTRO ==="
    exit 1
fi

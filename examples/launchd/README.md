# Running squeezy under launchd (macOS)

[`local.squeezy.plist`](local.squeezy.plist) is an example LaunchAgent that
starts squeezy at login and restarts it if it exits.

## Install

```bash
mkdir -p ~/Library/LaunchAgents
cp local.squeezy.plist ~/Library/LaunchAgents/
# edit ProgramArguments: squeezy's path, player name, -s SERVER if needed
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.squeezy.plist
```

Stop it (also needed before re-running `bootstrap` after editing the plist):

```bash
launchctl bootout gui/$(id -u)/local.squeezy
```

Warnings and errors go to `/tmp/squeezy.log` (`StandardErrorPath`), which is
cleared at reboot. For a permanent log use a full path such as
`/Users/you/Library/Logs/squeezy.log` — launchd doesn't expand `~`. Add `-v`
to `ProgramArguments` for more detail.

## ffmpeg

launchd starts programs with `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, which
doesn't include Homebrew's `/opt/homebrew/bin` or `/usr/local/bin`. If squeezy
can't find ffmpeg it exits with `ffmpeg not found` in the log, and with
`KeepAlive` launchd retries every 10 seconds.

The example sets `PATH` in `EnvironmentVariables`. Either of these works too,
and both take precedence over `PATH`:

- `--ffmpeg /opt/homebrew/bin/ffmpeg` in `ProgramArguments`
- `SQUEEZY_FFMPEG` set to that path in `EnvironmentVariables`

The order is `--ffmpeg`, then `SQUEEZY_FFMPEG`, then `PATH`. Either override
can be a full path or a bare name to look up on `PATH`.

## Player identity

squeezy uses the Mac's hardware network address as its LMS player ID, so the
player keeps its playlist and settings across restarts. Add `-m
aa:bb:cc:dd:ee:ff` to `ProgramArguments` to pin the ID explicitly. That's
useful if you want it to stay the same whatever network interfaces the Mac
has, or to keep an existing LMS player entry.

## Local Network permission

The LaunchAgent needs macOS Local Network access just like an interactive
run. If the log shows `[Errno 65] No route to host`, run `squeezy
--request-local-network` once with the same `squeezy` binary the plist starts.
See the main [README](../../README.md#macos-connection-failed-errno-65-no-route-to-host).

# Roblox Auto-Rejoiner

> [!NOTE]
> This repository is archived and read-only. The project remains available as-is, including the network-aware recovery added for issue #2, but no further maintenance is planned.

> [!IMPORTANT]
> This project supports the Windows Roblox client only.

Roblox Auto-Rejoiner watches every Roblox client you have open (multi-instance, one per account) and, when one
account is kicked or stops responding, closes only that account's window and reopens only that account. The other
accounts are never touched.

Accounts are reopened through ExploitStrap: the monitor presses that account's own **Launch** button on ExploitStrap's
**Multi Instance** page (Windows UI Automation - no mouse or keyboard). ExploitStrap signs the account in; the rejoiner
never reads any login. Keep ExploitStrap's settings window open (if it is closed, the monitor opens it with `-settings`).
The account joins whatever Place ID is set on that page.

- Which account a window belongs to comes from its own Player log (`Report game_join_loadtime: ... userid:N`), and which
  log belongs to which window from ExploitStrap's log (`Started Roblox (PID n)` / `Got log file as ...`).
- A kick is any `Disconnection Notification. Reason: N` other than 285 - 285 is every normal teleport.
- A kicked window is closed within about half a second: roughly 4 seconds after a disconnect, Roblox reloads the
  client, and that reload closed every other open client too.
- An account is never launched while a window of it is still in game (a second copy disconnects the first with 276).
- Optional: "Also reopen accounts whose window closes or crashes" (off by default).

If the connection is offline, recovery waits until a Roblox endpoint is reachable. A launch that does not bring the
account into a game within 2 minutes is retried, up to 3 times.

An optional Discord bot provides remote status, screenshots, incident notifications, pause/resume controls, and manual rejoins.

## Install

Install Python 3.10 or newer, then run:

```cmd
python -m pip install -r requirements.txt
```

## Configure and run

The easiest option is the GUI:

```cmd
python main_gui.py
```

Enter the rejoin delay, Discord bot token and notification channel ID (the Place ID is optional - it only names the game in Discord), then save the configuration. **Send Test Message** posts a test message to the channel through the running bot. Settings are stored in a local `config.json`; this file contains the bot token and should never be committed or shared.

The monitor and bot can also be run separately:

```cmd
python roblox_monitor.py
python discord_bot.py
```

Running either service for the first time creates `config.json` beside the scripts. The Discord fields are optional unless the bot is used.

## Discord bot setup

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Add a bot and copy its token into the GUI or `config.json`.
3. Invite it with the `bot` and `applications.commands` scopes and grant permission to send messages and attach files.
4. Set `CHANNEL_ID` for automatic incident notifications. Setting `GUILD_ID` is optional and makes slash-command updates appear immediately in that server.

Available commands:

| Command | Purpose |
| --- | --- |
| `/status` | Show every account's state and optionally attach a screenshot |
| `/current_screen` | Capture the Roblox window |
| `/current_game` | Show the configured experience and link |
| `/placeid` | Show the configured Place ID |
| `/crashes` | Show kicks, freezes and rejoins per account |
| `/uptime` | Show monitor uptime |
| `/pause` | Pause automatic monitoring and recovery |
| `/resume` | Resume monitoring |
| `/rejoin account:<name>` | Close that account's window and reopen it |

## How disconnect recovery works

Every running client's Player log (`%LOCALAPPDATA%\Roblox\logs`) is read from its start, so a window that was already
sitting on a kick screen when the monitor started is recovered too; a later `! Joining game` in the same window clears
it. When a kick appears, only that process is closed, the configured delay is applied, connectivity is checked, and the
account is launched through ExploitStrap until it is back in a game. Discord gets one message per kick, rejoin and failure.

## Build the Windows executable

Run `build.cmd`. PyInstaller writes the executable to `dist\main_gui.exe`.

## Disclaimer

This project is not affiliated with, maintained, authorized, endorsed, or sponsored by Roblox Corporation. Automated rejoining may violate an individual experience's rules or platform policies. Use it at your own risk.

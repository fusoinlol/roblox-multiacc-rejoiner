"""Multi-account Roblox monitor.

Every running Roblox client is matched to its own Player log, and the log tells which account it is
("Report game_join_loadtime: ... userid:N") and when it is disconnected - the "Disconnected" box: "Lost connection with
reason : ..." / "Client has been disconnected with reason: ...", or on older clients "Disconnection Notification. Reason: N" -
or a join fails ("[DFLog::NetworkClient] Failed to connect to server": the "Connection Failed ... (Error Code: 279)" box).
When one account is kicked, only that client is closed, and only that account is reopened, by pressing its
own Launch button on ExploitStrap's Multi Instance page. The other clients are never touched.

Two things learned the hard way (30 Sep 2026):
- about 4 seconds after a client is disconnected, Roblox reloads it, and that reload closed EVERY open client.
  So a kicked client is closed at once (the log is polled twice a second), before the reload can start.
- launching an account that is already running disconnects the running copy (reason 276), which triggers the same
  reload. An account is never launched while a client for it is alive.
"""
import base64
import ctypes
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from ctypes import wintypes
from datetime import datetime
from glob import glob

import psutil
import requests

from config_manager import get_config_path, load_config

try:
    import win32gui
    from PIL import ImageGrab

    HAS_SCREENSHOT_LIBS = True
except ImportError:
    HAS_SCREENSHOT_LIBS = False


DEFAULT_PLACE_ID = "PUT_PLACE_ID_HERE"
ROBLOX_EXE = "robloxplayerbeta.exe"
INTERNET_ENDPOINTS = (
    ("www.roblox.com", 443),
    ("apis.roblox.com", 443),
)
# every normal teleport (dungeon to dungeon, back to the lobby) disconnects with this reason on purpose
CLIENT_TELEPORT_REASON = 285
# a join that never connects writes no "Disconnection Notification" at all - the window just sits on the "Connection Failed ...
# No response from server (Error Code: 279)" box until someone presses Retry (30 Sep: one window sat there 5.5 hours, another
# from 8:26 PM, while the account list said "Playing"). Roblox writes this line when it gives up on the join:
CONNECT_FAILED_REASON = 279
CONNECT_FAILED_TEXT = "Failed to connect to the experience. No response from server."
REASON_NAMES = {
    0: "disconnected",
    267: "kicked by the game",
    276: "the same account joined somewhere else",
    277: "connection lost",
    279: "failed to connect",
}
POLL_SECONDS = 0.5
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,20}$")

RE_JOIN = re.compile(r"! Joining game '([0-9a-fA-F-]+)' place (\d+)")
RE_USERID = re.compile(r"Report game_join_loadtime: .*?\buserid:(\d+)")
RE_DISCONNECT = re.compile(r"Disconnection Notification\. Reason: (\d+)")
RE_CONNECT_FAILED = re.compile(r"\[DFLog::NetworkClient\] Failed to connect to server")
# what the error box on screen says: a game's own kick text, else Roblox's disconnect text
RE_KICK_TEXT = re.compile(r"Server Kick Message: (.*)")
RE_DISCONNECT_TEXT = re.compile(r"(?:Client has been disconnected with reason|Lost connection with reason)\s*:\s*(.*)")
# Roblox 0.741 (30 Sep 2026, evening) stopped writing "Disconnection Notification. Reason: N": the "Disconnected" box is now only
# in the RE_DISCONNECT_TEXT lines, with no code (1 Oct: a kick by the anti-cheat and one by hand both wrote them; ~40 teleports
# in the same logs never did - a teleport writes "Disconnected from server for reason: Player: 285"). The box's text gives the code.
DISCONNECT_TEXT_REASONS = (
    ("kicked", 267),
    ("different device", 276),
    ("connection", 277),
)
RE_ES_START = re.compile(r"Started Roblox \(PID (\d+)\)")
RE_ES_LOG = re.compile(r"Got log file as (.+?\.log)")

PLACE_ID = DEFAULT_PLACE_ID
REJOIN_DELAY = 5.0
SOCKET_HOST = "127.0.0.1"
SOCKET_PORT = 45678
SOCKET_AUTH = ""
GAME_NAME = "Not Set"
GAME_URL = "https://www.roblox.com/games/"
REOPEN_CLOSED = False
FREEZE_SECONDS = 45.0
LAUNCH_TIMEOUT = 120.0
LAUNCH_ATTEMPTS = 3
ROBLOX_LOG_DIR = ""
EXPLOITSTRAP_DIR = ""

MONITOR_ACTIVE = True
SOCKET_ACTIVE = True


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%I:%M:%S %p")
    print(f"[{timestamp}] {message}")


def _positive_number(value, fallback: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(value))
    except (TypeError, ValueError):
        return fallback


def _valid_place_id(place_id: str) -> bool:
    return bool(place_id and place_id != DEFAULT_PLACE_ID and place_id.isdigit())


def clean_message(text: str) -> str:
    text = re.split(r";\s*Metadata:", text, maxsplit=1)[0]
    text = " ".join(text.split())
    return text[:300]


def reason_text(reason: int | None) -> str:
    if reason is None:
        return "unknown reason"
    if reason == 0:
        return REASON_NAMES[0]
    return f"{REASON_NAMES.get(reason, 'disconnected')} (code {reason})"


def reason_from_text(text: str) -> int:
    """The code for a "Disconnected" box that only has its text in the log (Roblox 0.741 and later)."""
    lowered = (text or "").lower()
    for words, reason in DISCONNECT_TEXT_REASONS:
        if words in lowered:
            return reason
    return 0


def get_game_name(place_id: str) -> str:
    """Return the Roblox experience name, with the place ID as a safe fallback."""
    if not _valid_place_id(place_id):
        return "Not Set"

    headers = {"User-Agent": "Roblox-Auto-Rejoiner/2.0"}
    try:
        response = requests.get(
            f"https://apis.roblox.com/universes/v1/places/{place_id}/universe",
            headers=headers,
            timeout=5,
        )
        response.raise_for_status()
        universe_id = response.json().get("universeId")
        if universe_id:
            response = requests.get(
                f"https://games.roblox.com/v1/games?universeIds={universe_id}",
                headers=headers,
                timeout=5,
            )
            response.raise_for_status()
            games = response.json().get("data", [])
            if games and games[0].get("name"):
                return games[0]["name"]
    except (requests.RequestException, ValueError, TypeError) as exc:
        log(f"Could not fetch the game name: {exc}")

    return f"Place {place_id}"


state = {
    "monitoring": True,
    "roblox_running": False,
    "crash_count": 0,
    "freeze_count": 0,
    "disconnect_count": 0,
    "last_crash": None,
    "last_freeze": None,
    "last_disconnect": None,
    "last_disconnect_reason": None,
    "last_rejoin": None,
    "monitor_start": datetime.now().isoformat(),
    "place_id": PLACE_ID,
    "game_name": GAME_NAME,
    "game_url": GAME_URL,
    "status_message": "Starting up...",
    "accounts": {},
    "events": [],
    "event_seq": 0,
}
state_lock = threading.Lock()
_launch_lock = threading.Lock()
_workers: dict[int, threading.Thread] = {}
_names: dict[int, str] = {}
_manual_requests: list[int] = []


def reload_monitor_config(fetch_game_name: bool = False) -> None:
    """Reload settings without requiring either service to be re-imported."""
    global PLACE_ID, REJOIN_DELAY, SOCKET_HOST, SOCKET_PORT, SOCKET_AUTH, GAME_NAME, GAME_URL
    global REOPEN_CLOSED, FREEZE_SECONDS, LAUNCH_TIMEOUT, LAUNCH_ATTEMPTS, ROBLOX_LOG_DIR, EXPLOITSTRAP_DIR

    config = load_config()
    PLACE_ID = str(config.get("PLACE_ID", DEFAULT_PLACE_ID)).strip()
    REJOIN_DELAY = _positive_number(config.get("REJOIN_DELAY"), 5.0)
    SOCKET_HOST = str(config.get("SOCKET_HOST", "127.0.0.1"))
    try:
        SOCKET_PORT = int(config.get("SOCKET_PORT", 45678))
    except (TypeError, ValueError):
        SOCKET_PORT = 45678
    SOCKET_AUTH = str(config.get("SOCKET_AUTH", ""))
    REOPEN_CLOSED = bool(config.get("REOPEN_CLOSED", False))
    FREEZE_SECONDS = _positive_number(config.get("FREEZE_SECONDS"), 45.0, 10.0)
    LAUNCH_TIMEOUT = _positive_number(config.get("LAUNCH_TIMEOUT"), 120.0, 30.0)
    LAUNCH_ATTEMPTS = int(_positive_number(config.get("LAUNCH_ATTEMPTS"), 3, 1))
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    ROBLOX_LOG_DIR = str(config.get("ROBLOX_LOG_DIR") or os.path.join(local_app_data, "Roblox", "logs"))
    EXPLOITSTRAP_DIR = str(config.get("EXPLOITSTRAP_DIR") or os.path.join(local_app_data, "ExploitStrap"))
    GAME_NAME = get_game_name(PLACE_ID) if fetch_game_name else (
        "Not Set" if not _valid_place_id(PLACE_ID) else f"Place {PLACE_ID}"
    )
    GAME_URL = f"https://www.roblox.com/games/{PLACE_ID}"

    with state_lock:
        state.update(place_id=PLACE_ID, game_name=GAME_NAME, game_url=GAME_URL)


reload_monitor_config()


# --------------------------------------------------------------------------------------------------------------------
# events and accounts (read by the GUI and, over the socket, by the Discord bot)

def add_event(kind: str, account: str, detail: str) -> None:
    now = datetime.now().isoformat()
    with state_lock:
        state["event_seq"] += 1
        state["events"].append({"id": state["event_seq"], "time": now, "kind": kind, "account": account, "detail": detail})
        del state["events"][:-50]
    log(f"{account}: {detail}")


def _account(user_id: int) -> dict:
    """The account's record (state_lock must be held)."""
    key = str(user_id)
    acct = state["accounts"].get(key)
    if acct is None:
        acct = {
            "user_id": user_id,
            "name": _names.get(user_id) or f"user {user_id}",
            "status": "Playing",
            "pid": None,
            "place_id": None,
            "kicks": 0,
            "rejoins": 0,
            "last_kick": None,
            "last_reason": None,
            "last_message": None,
            "last_rejoin": None,
            "detail": "",
        }
        state["accounts"][key] = acct
    return acct


def set_account(user_id: int, **fields) -> None:
    with state_lock:
        _account(user_id).update(fields)


def account_name(user_id: int | None) -> str:
    if user_id is None:
        return "Unknown account"
    return _names.get(user_id) or f"user {user_id}"


def _lookup_name(user_id: int) -> None:
    try:
        response = requests.get(
            f"https://users.roblox.com/v1/users/{user_id}",
            headers={"User-Agent": "Roblox-Auto-Rejoiner/2.0"},
            timeout=6,
        )
        response.raise_for_status()
        name = str(response.json().get("name") or "")
    except (requests.RequestException, ValueError, TypeError) as exc:
        log(f"Could not look up the name of user {user_id}: {exc}")
        return
    if USERNAME_PATTERN.match(name):
        _names[user_id] = name
        set_account(user_id, name=name)


def ensure_name(user_id: int, wait: bool = False) -> str | None:
    if user_id in _names:
        return _names[user_id]
    if wait:
        _lookup_name(user_id)
        return _names.get(user_id)
    threading.Thread(target=_lookup_name, args=(user_id,), daemon=True).start()
    return None


# --------------------------------------------------------------------------------------------------------------------
# reading logs

class LogTail:
    """Reads a log from the start, then only what was added since the last read."""

    def __init__(self, path: str):
        self.path = path
        self.position = 0
        self.pending = ""

    def read_lines(self, limit: int = 8 * 1024 * 1024) -> list[str]:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size < self.position:
            self.position, self.pending = 0, ""
        if size == self.position:
            return []
        try:
            with open(self.path, "rb") as log_file:
                log_file.seek(self.position)
                data = log_file.read(limit)
                self.position = log_file.tell()
        except OSError:
            return []
        text = self.pending + data.decode("utf-8", errors="ignore")
        lines = text.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self.pending = lines.pop()
        else:
            self.pending = ""
        return lines


class Client:
    """One running Roblox client and what its own log says about it."""

    def __init__(self, pid: int, log_path: str):
        self.pid = pid
        self.log = LogTail(log_path)
        self.user_id: int | None = None
        self.place_id: int | None = None
        self.job_id: str | None = None
        self.joined_at: float | None = None
        self.kicked_reason: int | None = None
        self.kick_text: str | None = None
        self.kick_text_from_game = False
        self.connect_failed = False
        self.hung_since: float | None = None
        self.closing = False

    def _note_message(self, line: str) -> None:
        """Keep the text of the error box: the game's own kick message wins over Roblox's generic one."""
        match = RE_KICK_TEXT.search(line)
        if match:
            text = clean_message(match.group(1))
            if text:
                self.kick_text, self.kick_text_from_game = text, True
            return
        match = RE_DISCONNECT_TEXT.search(line)
        if match and not self.kick_text_from_game:
            text = clean_message(match.group(1))
            if text:
                self.kick_text = text

    def read_message(self) -> str | None:
        """After the window is closed: read the rest of its log for the error box text."""
        for line in self.log.read_lines():
            if "Kick Message" in line or "with reason" in line:
                self._note_message(line)
        return self.kick_text

    def poll(self) -> list[tuple[str, int | None]]:
        events = []
        for line in self.log.read_lines():
            if "game_join_loadtime" in line:
                match = RE_USERID.search(line)
                if match:
                    self.user_id = int(match.group(1))
            elif "! Joining game" in line:
                match = RE_JOIN.search(line)
                if match:
                    self.job_id, self.place_id = match.group(1), int(match.group(2))
                self.joined_at = time.monotonic()
                self.kicked_reason = None
                self.kick_text, self.kick_text_from_game = None, False
                self.connect_failed = False
                events.append(("joined", None))
            elif RE_CONNECT_FAILED.search(line):
                self.connect_failed = True
            elif "Kick Message" in line or "with reason" in line:
                self._note_message(line)
                # the "Disconnected" box (see DISCONNECT_TEXT_REASONS) - a game's kick text comes in the line before it
                match = RE_DISCONNECT_TEXT.search(line)
                if match and self.kicked_reason is None:
                    reason = reason_from_text(match.group(1))
                    self.kicked_reason = reason
                    events.append(("kicked", reason))
            elif "Disconnection Notification" in line:
                match = RE_DISCONNECT.search(line)
                if match and self.kicked_reason is None:
                    reason = int(match.group(1))
                    if reason != CLIENT_TELEPORT_REASON:
                        self.kicked_reason = reason
                        events.append(("kicked", reason))
        # a failed join counts only if nothing joined after it (a log read from the start, or Retry pressed by hand, can have
        # a failure followed by a good join)
        if self.connect_failed and self.kicked_reason is None:
            self.connect_failed = False
            self.kicked_reason = CONNECT_FAILED_REASON
            if not self.kick_text:
                self.kick_text = CONNECT_FAILED_TEXT
            events.append(("kicked", CONNECT_FAILED_REASON))
        return events


class ClientLocator:
    """Finds which Player log belongs to which Roblox process.

    ExploitStrap writes "Started Roblox (PID n)" and "Got log file as <path>" for every client it starts - including
    the reloads Roblox asks it for. Anything else falls back to asking Windows which log the process holds open.
    """

    def __init__(self):
        self.pid_to_log: dict[int, str] = {}
        self._tails: dict[str, LogTail] = {}
        self._last_pid: dict[str, int] = {}
        self._fallback_at: dict[int, float] = {}

    def scan_exploitstrap(self) -> None:
        cutoff = time.time() - 3 * 86400
        for path in glob(os.path.join(EXPLOITSTRAP_DIR, "Logs", "*.log")):
            try:
                if os.path.getmtime(path) < cutoff:
                    continue
            except OSError:
                continue
            tail = self._tails.get(path)
            if tail is None:
                tail = self._tails[path] = LogTail(path)
            for line in tail.read_lines():
                start = RE_ES_START.search(line)
                if start:
                    self._last_pid[path] = int(start.group(1))
                    continue
                found = RE_ES_LOG.search(line)
                if found and path in self._last_pid:
                    self.pid_to_log[self._last_pid.pop(path)] = os.path.expandvars(found.group(1).strip())

    def log_for(self, pid: int) -> str | None:
        path = self.pid_to_log.get(pid)
        if path:
            return path
        now = time.monotonic()
        if now - self._fallback_at.get(pid, -1e9) < 20:
            return None
        self._fallback_at[pid] = now
        try:
            for opened in psutil.Process(pid).open_files():
                name = os.path.basename(opened.path).lower()
                if name.endswith(".log") and "player" in name:
                    self.pid_to_log[pid] = opened.path
                    return opened.path
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            pass
        return None

    def forget(self, pid: int) -> None:
        self.pid_to_log.pop(pid, None)
        self._fallback_at.pop(pid, None)


# --------------------------------------------------------------------------------------------------------------------
# processes and windows

def running_clients() -> set[int]:
    pids = set()
    for process in psutil.process_iter(["name"]):
        try:
            if (process.info.get("name") or "").lower() == ROBLOX_EXE:
                pids.add(process.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


def close_client(pid: int) -> None:
    """Close one client (and any Roblox process it started) - never the others."""
    try:
        process = psutil.Process(pid)
        family = [process] + [
            child for child in process.children(recursive=True)
            if (child.name() or "").lower() == ROBLOX_EXE
        ]
        for member in family:
            try:
                member.kill()
            except psutil.NoSuchProcess:
                pass
    except psutil.NoSuchProcess:
        return
    except psutil.AccessDenied:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=10, check=False)


if os.name == "nt":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
else:
    _user32 = None


def is_client_hung(pid: int) -> bool:
    """True when a visible window of this client is marked Not Responding by Windows."""
    if _user32 is None:
        return False
    hung = []

    def callback(hwnd, _lparam):
        owner = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and _user32.IsWindowVisible(hwnd) and _user32.IsHungAppWindow(hwnd):
            hung.append(hwnd)
            return False
        return True

    _user32.EnumWindows(_EnumWindowsProc(callback), 0)
    return bool(hung)


def take_screenshot() -> str:
    if not HAS_SCREENSHOT_LIBS:
        return "ERR: Pillow or pywin32 is not installed"

    window = win32gui.FindWindow(None, "Roblox") or win32gui.FindWindow("Win32Window0", "Roblox")
    if not window:
        return "ERR: Roblox window not found"

    try:
        ctypes.windll.user32.SetProcessDPIAware()
        rect = win32gui.GetWindowRect(window)
        if rect[0] <= -32000:
            return "ERR: Roblox window is minimized"
        screenshot = ImageGrab.grab(bbox=rect, all_screens=True)
        path = os.path.join(os.path.dirname(get_config_path()), "roblox_current.png")
        screenshot.save(path)
        return path
    except Exception as exc:
        return f"ERR: {exc}"


def internet_available(timeout: float = 3.0) -> bool:
    """Check that a Roblox endpoint is reachable, including working DNS."""
    for endpoint in INTERNET_ENDPOINTS:
        try:
            with socket.create_connection(endpoint, timeout=timeout):
                return True
        except OSError:
            continue
    return False


# --------------------------------------------------------------------------------------------------------------------
# reopening one account through ExploitStrap

# Presses the account's own Launch button on ExploitStrap's Multi Instance page through Windows UI Automation (no mouse
# or keyboard, no focus change). ExploitStrap signs the account in itself - no login is ever read here.
LAUNCH_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$A = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$CT = [System.Windows.Automation.ControlType]
$user = '__USER__'
$exe = '__EXE__'
# ExploitStrap's processes, whatever its exe is called (a renamed "ExploitStrap (1).exe" is not "ExploitStrap")
function Strap-Processes { Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -like '*strap*' } }
# its window: any top-level window titled "ExploitStrap..." that belongs to one of those processes (the Settings one first). The
# process's "main window" alone missed it on another PC while the window was open on screen ("the ExploitStrap window is not open")
function Find-Window {
  $ids = @{}
  foreach ($p in Strap-Processes) { $ids[[int]$p.Id] = $true }
  if ($ids.Count -eq 0) { return $null }
  $tops = $A::RootElement.FindAll($TS::Children, [System.Windows.Automation.Condition]::TrueCondition)
  foreach ($want in @('ExploitStrap*Settings*', 'ExploitStrap*')) {
    foreach ($e in $tops) {
      try { if ($ids.ContainsKey([int]$e.Current.ProcessId) -and $e.Current.Name -like $want) { return $e } } catch {}
    }
  }
  return $null
}
# Windows hides an administrator program's window from a normal one: ExploitStrap run as administrator, the rejoiner not
function Strap-Elevated {
  $me = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  if ($me) { return $false }
  foreach ($p in Strap-Processes) { try { if (-not $p.Path) { return $true } } catch { return $true } }
  return $false
}
function Find-Launch($win) {
  $all = $win.FindAll($TS::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
  $found = $false
  foreach ($e in $all) {
    $n = $e.Current.Name
    $t = $e.Current.ControlType
    if (-not $found) { if ($n -eq ('@' + $user)) { $found = $true }; continue }
    if ($t -eq $CT::Text -and $n -like '@*') { return $null }
    if ($t -eq $CT::Button -and $n -eq 'Launch') { return $e }
  }
  return $null
}
function Open-MultiInstance($win) {
  $cond = New-Object System.Windows.Automation.PropertyCondition($A::NameProperty, 'Multi Instance')
  $e = $win.FindFirst($TS::Descendants, $cond)
  $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
  for ($i = 0; $e -and $i -lt 6; $i++) {
    foreach ($pat in $e.GetSupportedPatterns()) {
      if ($pat.ProgrammaticName -eq 'InvokePatternIdentifiers.Pattern') { $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); return $true }
      if ($pat.ProgrammaticName -eq 'SelectionItemPatternIdentifiers.Pattern') { $e.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select(); return $true }
    }
    $e = $walker.GetParent($e)
  }
  return $false
}
$win = Find-Window
if (-not $win -and (Test-Path $exe)) {
  Start-Process -FilePath $exe -ArgumentList '-settings'
  for ($i = 0; $i -lt 40 -and -not $win; $i++) { Start-Sleep -Milliseconds 500; $win = Find-Window }
}
if (-not $win) {
  if (Strap-Elevated) { 'ERR: ExploitStrap is running as administrator - run the rejoiner as administrator too'; exit 2 }
  if (Strap-Processes) { "ERR: ExploitStrap is running but its window is closed (in the tray?) - open it on the Multi Instance page"; exit 2 }
  'ERR: the ExploitStrap window is not open'; exit 2
}
$btn = Find-Launch $win
if (-not $btn) {
  if (Open-MultiInstance $win) { Start-Sleep -Milliseconds 1500; $btn = Find-Launch $win }
}
if (-not $btn -and (Strap-Elevated)) { 'ERR: ExploitStrap is running as administrator - run the rejoiner as administrator too'; exit 3 }
if (-not $btn) { "ERR: no Launch button for @$user on ExploitStrap's Multi Instance page"; exit 3 }
$btn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
'OK'
"""


def launch_account(username: str) -> tuple[bool, str]:
    if not USERNAME_PATTERN.match(username or ""):
        return False, "not a Roblox username"
    exe = os.path.join(EXPLOITSTRAP_DIR, "ExploitStrap.exe").replace("'", "''")
    script = LAUNCH_SCRIPT.replace("__USER__", username).replace("__EXE__", exe)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run PowerShell: {exc}"
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    last = lines[-1] if lines else (result.stderr or "").strip()[:200]
    return last == "OK", last or "no answer from PowerShell"


# --------------------------------------------------------------------------------------------------------------------
# the monitor

clients: dict[int, Client] = {}
clients_lock = threading.Lock()


def _monitoring_enabled() -> bool:
    with state_lock:
        return bool(state["monitoring"])


def _controlled_delay(seconds: float, pause_aware: bool = True) -> bool:
    deadline = time.monotonic() + seconds
    while MONITOR_ACTIVE and time.monotonic() < deadline:
        if pause_aware and not _monitoring_enabled():
            time.sleep(0.25)
            deadline += 0.25
            continue
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    return MONITOR_ACTIVE


def wait_for_internet(pause_aware: bool = True) -> bool:
    announced = False
    while MONITOR_ACTIVE:
        if pause_aware and not _monitoring_enabled():
            time.sleep(0.25)
            continue
        if internet_available():
            if announced:
                log("Internet connection restored.")
            return True
        if not announced:
            log("No Roblox network connection. Waiting before reopening...")
            announced = True
        if not _controlled_delay(5, pause_aware=pause_aware):
            break
    return False


def live_client_for(user_id: int, joined_after: float | None = None) -> Client | None:
    """A running client of this account that is in a game (and joined after the given time)."""
    with clients_lock:
        for client in clients.values():
            if client.user_id != user_id or client.closing or client.kicked_reason is not None:
                continue
            if joined_after is not None and (client.joined_at is None or client.joined_at < joined_after):
                continue
            return client
    return None


def _rejoin_worker(user_id: int, why: str) -> None:
    """Reopen one account, only once nothing of it is running any more."""
    try:
        name = ensure_name(user_id, wait=True)
        if not name:
            set_account(user_id, status="Closed", detail="name unknown - reopen it by hand")
            add_event("gave_up", account_name(user_id), "could not look up its username, so it cannot be reopened")
            return
        set_account(user_id, status="Reopening", detail=f"{why}; reopening in {REJOIN_DELAY:g}s")
        if not _controlled_delay(REJOIN_DELAY):
            return
        started = time.monotonic()
        for attempt in range(1, LAUNCH_ATTEMPTS + 1):
            if live_client_for(user_id, joined_after=started - REJOIN_DELAY - 1):
                break
            if not wait_for_internet():
                return
            set_account(user_id, status="Reopening", detail=f"launching (try {attempt} of {LAUNCH_ATTEMPTS})")
            with _launch_lock:
                if live_client_for(user_id):
                    break
                launched_at = time.monotonic()
                ok, message = launch_account(name)
            if not ok:
                add_event("rejoin_failed", name, f"ExploitStrap launch failed: {message}")
                if not _controlled_delay(15):
                    return
                continue
            deadline = launched_at + LAUNCH_TIMEOUT
            while MONITOR_ACTIVE and time.monotonic() < deadline:
                if live_client_for(user_id, joined_after=launched_at):
                    break
                time.sleep(1)
            if live_client_for(user_id, joined_after=launched_at):
                break
            add_event("rejoin_failed", name, f"launched, but it was not in a game after {LAUNCH_TIMEOUT:g}s")
        else:
            set_account(user_id, status="Closed", detail="could not reopen it")
            add_event("gave_up", name, f"gave up after {LAUNCH_ATTEMPTS} launches - reopen it by hand")
            return
        now = datetime.now().isoformat()
        with state_lock:
            acct = _account(user_id)
            acct.update(status="Playing", detail="", last_rejoin=now)
            acct["rejoins"] += 1
            state["last_rejoin"] = now
        add_event("rejoined", name, "is back in game")
    finally:
        _workers.pop(user_id, None)


def start_rejoin(user_id: int, why: str) -> bool:
    worker = _workers.get(user_id)
    if worker and worker.is_alive():
        return False
    worker = threading.Thread(target=_rejoin_worker, args=(user_id, why), daemon=True)
    _workers[user_id] = worker
    worker.start()
    return True


def _on_kick(client: Client, reason: int) -> None:
    client.closing = True
    close_client(client.pid)
    # the window is closed at once (before Roblox can reload it); the error box text is read from its log afterwards
    message = client.read_message()
    said = f': "{message}"' if message else ""
    now = datetime.now().isoformat()
    name = account_name(client.user_id)
    with state_lock:
        state["disconnect_count"] += 1
        state["last_disconnect"] = now
        state["last_disconnect_reason"] = reason_text(reason) + said
        if client.user_id is not None:
            acct = _account(client.user_id)
            acct["kicks"] += 1
            acct.update(status="Kicked", pid=None, last_kick=now, last_reason=reason_text(reason), last_message=message)
    if client.user_id is None:
        add_event("kick", name, f"{reason_text(reason)}{said} - before it joined a game; closed its window")
        return
    add_event("kick", name, f"{reason_text(reason)}{said} - closed its window, reopening it in {REJOIN_DELAY:g}s")
    start_rejoin(client.user_id, "kicked")


def _on_freeze(client: Client) -> None:
    client.closing = True
    close_client(client.pid)
    now = datetime.now().isoformat()
    with state_lock:
        state["freeze_count"] += 1
        state["last_freeze"] = now
        if client.user_id is not None:
            _account(client.user_id).update(status="Frozen", pid=None)
    add_event("freeze", account_name(client.user_id), f"stopped responding for {FREEZE_SECONDS:g}s - closed its window")
    if client.user_id is not None:
        start_rejoin(client.user_id, "frozen")


def _on_gone(client: Client) -> None:
    if client.closing or client.user_id is None:
        return
    if live_client_for(client.user_id):
        return
    now = datetime.now().isoformat()
    with state_lock:
        state["crash_count"] += 1
        state["last_crash"] = now
        _account(client.user_id).update(status="Closed", pid=None)
    if REOPEN_CLOSED:
        add_event("closed", account_name(client.user_id), "its window closed - reopening it")
        start_rejoin(client.user_id, "closed")
    else:
        add_event("closed", account_name(client.user_id), "its window closed (not reopening: 'Reopen closed accounts' is off)")


def request_manual_rejoin(name: str) -> tuple[bool, str]:
    wanted = (name or "").strip().lstrip("@").lower()
    with state_lock:
        matches = [int(uid) for uid, acct in state["accounts"].items() if str(acct.get("name", "")).lower() == wanted]
    if not matches:
        return False, f"no account called {name} has been seen"
    user_id = matches[0]
    worker = _workers.get(user_id)
    if worker and worker.is_alive():
        return False, f"{account_name(user_id)} is already being reopened"
    with clients_lock:
        own = [client for client in clients.values() if client.user_id == user_id and not client.closing]
    for client in own:
        client.closing = True
        close_client(client.pid)
    add_event("manual", account_name(user_id), "rejoin asked for - closed its window, reopening it")
    start_rejoin(user_id, "rejoin asked for")
    return True, f"reopening {account_name(user_id)}"


def _update_summary() -> None:
    with clients_lock:
        live = [client for client in clients.values() if not client.closing]
    with state_lock:
        state["roblox_running"] = bool(live)
        for client in live:
            if client.user_id is None:
                continue
            acct = _account(client.user_id)
            worker = _workers.get(client.user_id)
            if client.kicked_reason is None and client.joined_at is not None and not (worker and worker.is_alive()):
                acct["status"] = "Playing"
                acct["detail"] = ""
            acct["pid"] = client.pid
            acct["place_id"] = client.place_id
        playing = sum(1 for acct in state["accounts"].values() if acct["status"] == "Playing")
        state["status_message"] = f"Monitoring {playing} account(s)" if state["monitoring"] else "Paused"


def monitor_loop() -> None:
    global MONITOR_ACTIVE
    MONITOR_ACTIVE = True
    reload_monitor_config(fetch_game_name=True)
    locator = ClientLocator()
    with clients_lock:
        clients.clear()
    log("Monitor started. Watching every Roblox client for kicks...")
    with state_lock:
        state["monitor_start"] = datetime.now().isoformat()
        state["status_message"] = "Monitoring"

    last_hung_check = 0.0
    while MONITOR_ACTIVE:
        if not _monitoring_enabled():
            with state_lock:
                state["status_message"] = "Paused"
            time.sleep(0.25)
            continue

        pids = running_clients()
        locator.scan_exploitstrap()
        for pid in pids:
            if pid in clients:
                continue
            path = locator.log_for(pid)
            if path:
                with clients_lock:
                    clients[pid] = Client(pid, path)
        for pid in [pid for pid in clients if pid not in pids]:
            with clients_lock:
                gone = clients.pop(pid)
            locator.forget(pid)
            _on_gone(gone)

        check_hung = time.monotonic() - last_hung_check > 2
        if check_hung:
            last_hung_check = time.monotonic()
        for client in list(clients.values()):
            if client.closing:
                continue
            for kind, value in client.poll():
                if kind == "kicked":
                    _on_kick(client, value)
                    break
                if kind == "joined" and client.user_id is not None:
                    ensure_name(client.user_id)
            if client.closing or client.user_id is None:
                continue
            ensure_name(client.user_id)
            if check_hung:
                if is_client_hung(client.pid):
                    client.hung_since = client.hung_since or time.monotonic()
                    if time.monotonic() - client.hung_since >= FREEZE_SECONDS:
                        _on_freeze(client)
                else:
                    client.hung_since = None

        _update_summary()
        time.sleep(POLL_SECONDS)

    with state_lock:
        state["status_message"] = "Stopped"
    log("Monitor stopped.")


# --------------------------------------------------------------------------------------------------------------------
# local control socket (used by the Discord bot)

def _decode_command(raw_data: str) -> str | None:
    if not SOCKET_AUTH:
        return raw_data
    token, separator, command = raw_data.partition(":")
    if separator and secrets.compare_digest(token, SOCKET_AUTH):
        return command
    return None


def handle_client(connection: socket.socket, _address) -> None:
    try:
        connection.settimeout(5)
        raw_data = connection.recv(2048).decode("utf-8", errors="replace").strip()
        command = _decode_command(raw_data)
        if command is None:
            connection.sendall(b"ERR: Unauthorized")
        elif command == "GET_STATE":
            with state_lock:
                payload = json.dumps(state)
            connection.sendall(payload.encode("utf-8"))
        elif command == "PAUSE":
            with state_lock:
                state["monitoring"] = False
                state["status_message"] = "Paused"
            connection.sendall(b"OK: Monitoring paused")
        elif command == "RESUME":
            with state_lock:
                state["monitoring"] = True
                state["status_message"] = "Monitoring"
            connection.sendall(b"OK: Monitoring resumed")
        elif command.startswith("REJOIN:"):
            ok, message = request_manual_rejoin(command[len("REJOIN:"):])
            connection.sendall((("OK: " if ok else "ERR: ") + message).encode("utf-8"))
        elif command == "GET_SCREENSHOT":
            connection.sendall(take_screenshot().encode("utf-8"))
        else:
            connection.sendall(b"ERR: Unknown command")
    except (OSError, ValueError) as exc:
        log(f"Socket client error: {exc}")
    finally:
        connection.close()


def socket_server() -> None:
    global SOCKET_ACTIVE
    SOCKET_ACTIVE = True
    log(f"Socket server listening on {SOCKET_HOST}:{SOCKET_PORT}")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((SOCKET_HOST, SOCKET_PORT))
            server.listen()
            server.settimeout(1)
            while SOCKET_ACTIVE:
                try:
                    connection, address = server.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=handle_client, args=(connection, address), daemon=True).start()
    except OSError as exc:
        log(f"Socket server error: {exc}")
    finally:
        SOCKET_ACTIVE = False


def main() -> None:
    print("=" * 50)
    print("  Roblox Auto-Rejoin Monitor (every account)")
    print("=" * 50)
    threading.Thread(target=socket_server, daemon=True).start()
    try:
        monitor_loop()
    except KeyboardInterrupt:
        globals()["MONITOR_ACTIVE"] = False
        globals()["SOCKET_ACTIVE"] = False
        log("Stopped by user.")


if __name__ == "__main__":
    main()

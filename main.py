#!/usr/bin/env python
"""
waybar-autohide — Hyprland Waybar autohide controller
======================================================

Hides Waybar when windows overlap the bar area, shows it when the cursor
approaches the top of the screen or no windows overlap. Designed for Hyprland
on Wayland.

HOW IT WORKS
------------
The main loop polls Hyprland every WAYBAR_AUTOHIDE_REFRESH_RATE seconds and
computes the desired WaybarState (VISIBLE or HIDDEN) based on two signals:

  1. Cursor proximity: if the cursor is within BAR_HEIGHT px of the top of any
     tracked monitor, show the bar.
  2. Window overlap: if any mapped, non-hidden, non-fullscreen window on an
     active workspace has its top edge within (BAR_HEIGHT + HEIGHT_THRESHOLD)
     px of y=0, hide the bar.

Visibility is controlled via SIGUSR1 (hide) and SIGUSR2 (show), which Waybar
responds to natively via its on-sigusr1/on-sigusr2 config keys.

MONITOR ADD / LID OPEN BUG AND FIX
------------------------------------
When a new monitor is added (e.g. opening a laptop lid while docked), Hyprland
fires a monitoradded IPC event and Waybar restarts to spawn a bar instance on
the new output. Without special handling, this causes a blank screen on the
newly-enabled display. The root cause is Hyprland's solitary/direct-scanout
optimization: when the only compositor surface on an output is a single window
(i.e. Waybar's layer surface has been collapsed by SIGUSR1), Hyprland attempts
direct scanout on that output. If the monitor was just added and the compositor
hasn't fully initialized it, the output shows nothing.

The fix uses a two-phase grace mechanism driven by Hyprland's IPC event stream:

  Phase 1 — monitoradded / monitoraddedv2:
    Set _monitor_grace = True. The main loop returns VISIBLE unconditionally,
    keeping Waybar's layer surface alive on all outputs while Hyprland
    initializes the new display.

  Phase 2 — openlayer>>waybar:
    Hyprland emits this event exactly when Waybar has successfully created its
    layer surface on the new output. This is the precise moment it is safe to
    resume normal autohide logic. We clear _monitor_grace and set _force_resync.

  Force resync:
    When grace clears, we can't simply rely on next_state != state to re-send
    the hide/show signal, because the script's internal state variable may
    already show HIDDEN from before the lid opened. If Waybar restarted
    mid-grace and the SIGUSR1 signal was lost, Waybar would be physically
    visible while the script thinks it's hidden — and would never re-send the
    signal. _force_resync causes the main loop to unconditionally re-apply the
    correct state on the next iteration, resynchronizing the script with
    Waybar's actual visual state.

The observed Hyprland IPC event sequence on lid open (from socket2):

    monitoradded>>eDP-1
    monitoraddedv2>>0,eDP-1,Sharp Corporation 0x1518
    openlayer>>waybar       <-- grace clears here, force_resync set
    openlayer>>wallpaper
    moveworkspace>>2,DVI-I-1
    moveworkspace>>1,DVI-I-1

openlayer>>waybar fires before workspaces have migrated, so at the moment
grace clears, windows are still on the new output. The overlap check correctly
returns HIDDEN, the force resync fires the signal, and by the time workspaces
settle the bar is already in the right state.

IPC SOCKET RESILIENCE
---------------------
The socket path includes HYPRLAND_INSTANCE_SIGNATURE, which changes if
Hyprland restarts. get_hyprland_socket2() is called inside the reconnect loop
(not once at startup) so the thread automatically picks up the new socket path
after a Hyprland restart without requiring a restart of this script.

ENVIRONMENT VARIABLES
---------------------
  WAYBAR_AUTOHIDE_BAR_HEIGHT       px height of the bar (default: 50)
  WAYBAR_AUTOHIDE_HEIGHT_THRESHOLD extra px below bar to trigger hide (default: 20)
  WAYBAR_AUTOHIDE_PROCNAME         process name to signal (default: waybar)
  WAYBAR_AUTOHIDE_REFRESH_RATE     poll interval in seconds (default: 0.25)
  WAYBAR_AUTOHIDE_MONITORS         comma-separated monitor IDs to track;
                                   empty = all monitors (default: empty)

LOGGING
-------
Logs to ~/.local/state/waybar-autohide.log at INFO level.
"""

import json
import logging
import os
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, fields
from enum import StrEnum
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Tuple


BAR_HEIGHT = int(os.getenv("WAYBAR_AUTOHIDE_BAR_HEIGHT", "50"))
HEIGHT_THRESHOLD = int(os.getenv("WAYBAR_AUTOHIDE_HEIGHT_THRESHOLD", "20"))
WAYBAR_PROC = os.getenv("WAYBAR_AUTOHIDE_PROCNAME", "waybar")
SUBPROCESS_TIMEOUT = 5  # seconds
WAYBAR_WAIT_TIMEOUT = 60  # seconds to wait for waybar at startup
LOG_FILE = Path.home() / ".local" / "state" / "waybar-autohide.log"


# Setup logging
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
    ]
)
log = logging.getLogger(__name__)


# Global for graceful shutdown
running = True

# Grace period state — see module docstring for full explanation.
#
# _monitor_grace: True while we're waiting for Waybar to open its layer on a
#   newly-added monitor. Prevents the main loop from hiding Waybar during the
#   window where Hyprland's solitary optimization would cause a blank screen.
#
# _force_resync: set to True when grace clears (openlayer>>waybar). Causes the
#   main loop to re-send the hide/show signal unconditionally once, recovering
#   from any desync between the script's internal state and Waybar's actual
#   visual state that may have occurred while Waybar restarted during grace.
_monitor_grace: bool = False
_grace_lock = threading.Lock()
_force_resync: bool = False


def set_monitor_grace():
    global _monitor_grace
    with _grace_lock:
        _monitor_grace = True
    log.info("monitoradded: grace period started, waiting for waybar layer")


def clear_monitor_grace():
    global _monitor_grace, _force_resync
    with _grace_lock:
        _monitor_grace = False
        _force_resync = True
    log.info("openlayer>>waybar: grace period cleared, resuming autohide")


def in_grace_period() -> bool:
    with _grace_lock:
        return _monitor_grace


def consume_force_resync() -> bool:
    """Returns True (and clears the flag) if a resync was requested."""
    global _force_resync
    with _grace_lock:
        if _force_resync:
            _force_resync = False
            return True
        return False


def get_hyprland_socket2() -> Optional[Path]:
    """
    Find the Hyprland IPC event socket (socket2) path.

    The path is derived from HYPRLAND_INSTANCE_SIGNATURE, which is unique per
    Hyprland session. Called on every reconnect attempt so that a Hyprland
    restart (new signature, new socket path) is handled automatically.
    """
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not sig:
        return None
    # Try /run/user/<uid>/hypr first, then /tmp/hypr
    uid = os.getuid()
    candidates = [
        Path(f"/run/user/{uid}/hypr/{sig}/.socket2.sock"),
        Path(f"/tmp/hypr/{sig}/.socket2.sock"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def monitor_ipc_events():
    """
    Background thread: stream Hyprland IPC events from socket2 and manage the
    grace period around monitor add/remove events.

    Relevant events handled:
      monitoradded / monitoraddedv2 — a new output was connected or enabled
        (e.g. laptop lid opened, external monitor plugged in). Starts grace.
      openlayer>>waybar — Waybar has opened its layer surface on the new output.
        Ends grace and sets force_resync.

    The socket path is re-resolved on every reconnection attempt so that if
    Hyprland restarts (new HYPRLAND_INSTANCE_SIGNATURE), the thread picks up
    the new socket automatically rather than hammering a stale path.
    """
    global running

    while running:
        sock_path = get_hyprland_socket2()
        if sock_path is None:
            log.warning("Could not find Hyprland socket2 — retrying in 2s...")
            time.sleep(2)
            continue

        try:
            log.info(f"Listening for Hyprland IPC events on {sock_path}")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.connect(str(sock_path))
                sock.settimeout(1.0)
                buf = ""
                while running:
                    try:
                        data = sock.recv(4096).decode("utf-8", errors="replace")
                        if not data:
                            break
                        buf += data
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            line = line.strip()
                            if line.startswith("monitoradded>>") or line.startswith("monitoraddedv2>>"):
                                monitor_name = line.split(">>", 1)[1]
                                log.info(f"monitoradded event: {monitor_name}")
                                set_monitor_grace()
                            elif line.startswith("openlayer>>waybar"):
                                clear_monitor_grace()
                    except socket.timeout:
                        continue
        except Exception as e:
            if running:
                log.warning(f"IPC listener error: {e}, reconnecting in 2s...")
                time.sleep(2)


def signal_handler(signum, frame):
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    global running
    running = False
    log.info(f"Received signal {signum}, shutting down...")
    # Show waybar before exiting so it's not left hidden
    show_waybar()


class WaybarState(StrEnum):
    VISIBLE = "1"
    HIDDEN = "0"


class BaseModel:
    @classmethod
    def model_validate(cls, data: dict):
        field_names = {(f.name, f.type) for f in fields(cls)}
        filtered_data = {}

        for field_name, field_type in field_names:
            if field_name not in data:
                continue

            if isinstance(field_type, type) and issubclass(field_type, BaseModel):
                filtered_data[field_name] = field_type.model_validate(data[field_name])
            elif isinstance(field_type, type):
                filtered_data[field_name] = field_type(data[field_name])
            else:
                filtered_data[field_name] = data[field_name]

        return cls(**filtered_data)


@dataclass
class Workspace(BaseModel):
    id: int
    name: str


@dataclass
class HyprlandClient(BaseModel):
    address: str
    mapped: bool
    hidden: bool

    at: Tuple[int, int]
    size: Tuple[int, int]

    workspace: Workspace
    monitor: int
    fullscreen: int


def is_waybar_running() -> bool:
    result = subprocess.run(
        ["pgrep", "-x", WAYBAR_PROC],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def is_hyprland_running() -> bool:
    result = subprocess.run(
        ["hyprctl", "version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def hide_waybar():
    """Send SIGUSR1 to hide waybar."""
    subprocess.run(["pkill", "-USR1", WAYBAR_PROC], check=False)


def show_waybar():
    """Send SIGUSR2 to show waybar."""
    subprocess.run(["pkill", "-USR2", WAYBAR_PROC], check=False)


def set_waybar_visibility(state: WaybarState):
    """Set waybar visibility to the given state (idempotent)."""
    if state == WaybarState.VISIBLE:
        show_waybar()
    else:
        hide_waybar()


def get_current_workspace() -> list[int]:
    """Return the active workspace ID for every active monitor."""
    try:
        workspaces = subprocess.check_output(
            "hyprctl monitors -j | jq '.[] | .activeWorkspace.id'",
            shell=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        return [int(workspace.strip()) for workspace in workspaces.strip().split("\n") if workspace.strip()]
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        return []


def get_monitor_from_position(pos_x: int, pos_y: int) -> Optional[int]:
    """Return the monitor ID that contains the given screen coordinates."""
    try:
        monitors = json.loads(subprocess.check_output(
            ["hyprctl", "-j", "monitors"],
            timeout=SUBPROCESS_TIMEOUT
        ))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None

    for monitor in monitors:
        start_x, end_x = monitor["x"], monitor["x"] + monitor["width"]
        start_y, end_y = monitor["y"], monitor["y"] + monitor["height"]

        if pos_x < start_x or pos_x > end_x:
            continue

        if pos_y < start_y or pos_y > end_y:
            continue

        return int(monitor["id"])

    return None


def get_clients(
    filter_func: Callable[[HyprlandClient], bool] | None = None,
) -> Iterator[HyprlandClient]:
    """Yield all Hyprland clients, optionally filtered."""
    try:
        raw_clients = json.loads(subprocess.check_output(
            ["hyprctl", "-j", "clients"],
            timeout=SUBPROCESS_TIMEOUT
        ))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return

    for c in raw_clients:
        try:
            client = HyprlandClient.model_validate(c)
            if filter_func is None or filter_func(client):
                yield client
        except (KeyError, TypeError, ValueError):
            continue


def get_overlapping_clients(
    active_workspaces: list[int],
    monitors: list[int] | None = None,
) -> Iterator[HyprlandClient]:
    """
    Yield clients whose top edge is within (BAR_HEIGHT + HEIGHT_THRESHOLD) px
    of y=0, meaning they overlap or nearly overlap the bar area.

    Skips unmapped, hidden, fullscreen, and off-workspace clients.
    """
    def overlaps_bar(c: HyprlandClient) -> bool:
        if not c.mapped:
            return False
        if c.hidden:
            return False
        if c.fullscreen:
            return False
        if c.monitor not in (monitors or [c.monitor]):
            return False
        if c.workspace.id not in active_workspaces:
            return False

        x, y = c.at
        w, h = c.size

        return y < (BAR_HEIGHT + HEIGHT_THRESHOLD) and (y + h) > 0

    return get_clients(overlaps_bar)


def window_overlaps_bar(
    active_workspaces: list[int], monitors: List[int] | None = None
) -> bool:
    return any(get_overlapping_clients(active_workspaces, monitors))


def get_cursor_position() -> Optional[Tuple[int, int]]:
    """Return the current cursor position as (x, y), or None on failure."""
    try:
        output = subprocess.check_output(
            ["hyprctl", "cursorpos"],
            timeout=SUBPROCESS_TIMEOUT
        ).decode()
        parts = output.strip().split(",")
        if len(parts) != 2:
            return None
        return int(parts[0].strip()), int(parts[1].strip())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        return None


def cursor_aproaches_bar(
    monitors: list[int] | None, current_state: WaybarState
) -> bool:
    """
    Return True if the cursor is close enough to the top of a tracked monitor
    to trigger the bar to show.

    When the bar is visible, the threshold is BAR_HEIGHT (cursor must be within
    the bar itself). When hidden, the threshold is 0 (cursor must be at y=0,
    i.e. the very top pixel) to avoid accidental triggers.
    """
    pos = get_cursor_position()
    if pos is None:
        return False

    x, y = pos
    cursor_monitor = get_monitor_from_position(x, y)
    if cursor_monitor is None or cursor_monitor not in (monitors or [cursor_monitor]):
        return False

    offset = BAR_HEIGHT if current_state == WaybarState.VISIBLE else 0

    return y <= offset


def get_next_state(
    waybar_monitors: list[int], current_state: WaybarState
) -> WaybarState:
    """
    Compute the desired Waybar visibility state.

    Priority order:
      1. Grace period active → VISIBLE (protect against blank screen on monitor add)
      2. Cursor near top of screen → VISIBLE
      3. Window overlaps bar area → HIDDEN
      4. Otherwise → VISIBLE
    """
    # During grace period after a monitor is added, keep waybar visible so
    # Hyprland has time to fully composite the new output before we collapse
    # the layer surface (which would otherwise trigger a blank screen).
    if in_grace_period():
        return WaybarState.VISIBLE

    cursor_aproaches = cursor_aproaches_bar(waybar_monitors, current_state)

    if cursor_aproaches:
        return WaybarState.VISIBLE

    active_workspaces = get_current_workspace()
    overlaps = window_overlaps_bar(active_workspaces, waybar_monitors)

    if overlaps:
        return WaybarState.HIDDEN

    return WaybarState.VISIBLE


def main():
    global running

    # Setup signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    waybar_monitors = os.environ.get("WAYBAR_AUTOHIDE_MONITORS", None)
    if waybar_monitors is not None:
        waybar_monitors = waybar_monitors.split(",")
        waybar_monitors = [int(m.strip()) for m in waybar_monitors if m.strip()]
    else:
        waybar_monitors = []

    # State tracks what visibility signal was last sent to Waybar. Used to
    # avoid redundant signals and to detect when the bar needs to change state.
    # Initialised to VISIBLE because Waybar starts visible on launch.
    state: WaybarState = WaybarState.VISIBLE

    refresh_rate = float(os.environ.get("WAYBAR_AUTOHIDE_REFRESH_RATE", "0.25"))

    if not is_hyprland_running():
        log.error("Hyprland is not running. Exiting.")
        return

    # Start IPC event listener thread for monitoradded grace period.
    # Daemon thread so it doesn't block process exit.
    ipc_thread = threading.Thread(target=monitor_ipc_events, daemon=True)
    ipc_thread.start()

    log.info("Waiting for waybar to start...")

    # Wait for waybar to start (with timeout)
    wait_start = time.time()
    while not is_waybar_running():
        if time.time() - wait_start > WAYBAR_WAIT_TIMEOUT:
            log.error(f"Waybar did not start within {WAYBAR_WAIT_TIMEOUT}s. Exiting.")
            return
        time.sleep(0.5)

    log.info("Waybar detected. Starting autohide loop.")

    while running:
        try:
            # If Waybar stopped (e.g. monitor topology change caused a restart),
            # wait for it to come back and reset state to VISIBLE since Waybar
            # always starts visible after a restart.
            if not is_waybar_running():
                log.warning("Waybar stopped. Waiting for restart...")
                while not is_waybar_running() and running:
                    time.sleep(0.5)
                if not running:
                    break
                state = WaybarState.VISIBLE
                log.info("Waybar restarted. State reset to VISIBLE.")
                continue

            next_state = get_next_state(waybar_monitors, state)

            # Apply state if it changed, OR if a force resync was requested
            # (set when the monitor grace period clears). The resync ensures
            # we re-send the correct signal even if state hasn't changed,
            # recovering from any desync caused by a signal being lost while
            # Waybar was restarting mid-grace.
            if next_state != state or consume_force_resync():
                set_waybar_visibility(next_state)  # idempotent - no desync possible
                state = next_state

        except Exception as e:
            # Log but don't crash on unexpected errors
            log.exception(f"Error in main loop: {e}")

        time.sleep(refresh_rate)

    # Ensure waybar is visible before exiting
    show_waybar()
    log.info("Shutting down gracefully.")


if __name__ == "__main__":
    main()

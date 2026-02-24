#!/usr/bin/env python

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

# Grace period: don't hide waybar until this flag is cleared.
# Set when monitoradded fires, cleared when Hyprland confirms Waybar has
# opened its layer surface on the new output (openlayer>>waybar event).
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
    """Find the Hyprland IPC socket2 path."""
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
    Background thread: connect to Hyprland's event socket2 and listen for
    'monitoradded' events. When one fires, set a grace period so the main
    loop won't hide Waybar until Hyprland has fully composited the new output.

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

    # State tracking for cursor detection (bar height changes based on visibility)
    state: WaybarState = WaybarState.VISIBLE  # waybar starts visible

    refresh_rate = float(os.environ.get("WAYBAR_AUTOHIDE_REFRESH_RATE", "0.25"))

    if not is_hyprland_running():
        log.error("Hyprland is not running. Exiting.")
        return

    # Start IPC event listener thread for monitoradded grace period
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
            # Check if waybar is still running
            if not is_waybar_running():
                log.warning("Waybar stopped. Waiting for restart...")
                # Wait for waybar to come back
                while not is_waybar_running() and running:
                    time.sleep(0.5)
                if not running:
                    break
                # Waybar restarted - it starts visible, resync state
                state = WaybarState.VISIBLE
                log.info("Waybar restarted. State reset to VISIBLE.")
                continue

            next_state = get_next_state(waybar_monitors, state)
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

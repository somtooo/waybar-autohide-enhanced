# Waybar Autohide

A lightweight Python script that automatically hides and shows [Waybar](https://github.com/Alexays/Waybar) based on window overlap and cursor position in [Hyprland](https://hyprland.org/), with robust handling for monitor hotplug and laptop lid events.

## Features

- **Auto-hide on overlap**: Hides Waybar when a window overlaps the bar area
- **Cursor reveal**: Shows Waybar when the cursor approaches the top of the screen
- **Multi-monitor support**: Limit autohide to specific monitor IDs
- **Hotplug-safe**: Prevents blank screens when a monitor is added (e.g., lid open)
- **Resync-safe**: Recovers from Waybar restarts without getting stuck visible
- **Configurable refresh rate**: Adjust how often the script checks for changes

## Requirements

- Python >= 3.12
- Hyprland window manager (hyprctl)
- Waybar
- jq

## Installation

```bash
# Install to ~/.local/bin (default)
make install

# Or specify a custom path
make install INSTALL_PATH=/usr/local/bin
```

## Usage

Simply run the script:

```bash
waybar-autohide
```

### Recommended Usage

Add to your Hyprland config to start on launch:

```
env = WAYBAR_AUTOHIDE_MONITORS,1
exec-once = waybar-autohide
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `WAYBAR_AUTOHIDE_MONITORS` | Comma-separated list of monitor IDs to enable auto-hide (e.g., `0,1`) | All monitors |
| `WAYBAR_AUTOHIDE_REFRESH_RATE` | Polling interval in seconds | `0.25` |
| `WAYBAR_AUTOHIDE_BAR_HEIGHT` | Height of the Waybar in pixels | `50` |
| `WAYBAR_AUTOHIDE_HEIGHT_THRESHOLD` | Additional threshold for overlap detection in pixels | `20` |
| `WAYBAR_AUTOHIDE_PROCNAME` | Process name of Waybar | `waybar` |

### Example

```bash
# Enable auto-hide only on monitor 0, with faster refresh
WAYBAR_AUTOHIDE_MONITORS=0 \
WAYBAR_AUTOHIDE_REFRESH_RATE=0.25 \
waybar-autohide
```


## How It Works

1. Polls Hyprland for active workspaces and window positions
2. Checks if any window overlaps with the Waybar area
3. Monitors cursor position to reveal the bar when the cursor approaches the top
4. Sends `SIGUSR1` to hide and `SIGUSR2` to show Waybar

Waybar must be configured with `on-sigusr1: hide` and `on-sigusr2: show`.

### Monitor Add / Lid Open Handling

When a monitor is added (e.g. opening a laptop lid while docked), Hyprland
emits `monitoradded` events and Waybar restarts to create a new bar instance.
If the script immediately hides Waybar, Hyprland's solitary/direct-scanout
optimization can blank the new output. To prevent that:

- The IPC listener watches Hyprland's socket2 events.
- `monitoradded` starts a grace period where the bar stays visible.
- `openlayer>>waybar` ends the grace period once Waybar's layer surface is live.
- A force-resync re-applies the correct hide/show signal after grace clears to
  recover from any signal lost during Waybar restart.

The socket path depends on `HYPRLAND_INSTANCE_SIGNATURE`, so the listener
re-resolves it on each reconnect attempt to survive Hyprland restarts.

## Development

```bash
# Install dev dependencies and sync
make setup-dev

# Lint the code
make lint

# Format the code
make format
```

## License

GPLv2

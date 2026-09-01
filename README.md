# Omarchy Bar Autohide

A lightweight Python script that automatically hides and shows the [Omarchy](https://omarchy.org/) status bar (Quickshell-based, Omarchy 4+) based on window overlap and cursor position in [Hyprland](https://hyprland.org/), with robust handling for monitor hotplug and laptop lid events.

## Features

- **Auto-hide on overlap**: Hides the bar when a window overlaps the bar area
- **Cursor reveal**: Shows the bar when the cursor reaches the top pixel of the screen
- **Multi-monitor support**: Limit autohide to specific monitor IDs
- **Hotplug-safe**: Keeps the bar visible during monitor add / lid open, then resyncs
- **State-adopting**: Respects the bar's actual state at startup (a manual `omarchy toggle bar` is not overridden)
- **Cheap toggling**: Uses Omarchy's `bar-off` park-off-screen mechanism — showing is a margin change, not a surface rebuild
- **Configurable refresh rate**: Adjust how often the script checks for changes

## Requirements

- Omarchy 4+ (the Quickshell-based `omarchy-shell` bar)
- Python >= 3.12
- Hyprland window manager (`hyprctl`)
- `jq`

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
omarchy-bar-autohide
```

### Recommended Usage

Omarchy configures Hyprland in Lua. Add to `~/.config/hypr/autostart.lua`:

```lua
o.launch_on_start("omarchy-bar-autohide")
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `OMARCHY_BAR_AUTOHIDE_MONITORS` | Comma-separated list of monitor IDs to enable auto-hide (e.g., `0,1`) | All monitors |
| `OMARCHY_BAR_AUTOHIDE_REFRESH_RATE` | Polling interval in seconds | `0.25` |
| `OMARCHY_BAR_AUTOHIDE_BAR_HEIGHT` | Height of the bar in pixels | `26` |
| `OMARCHY_BAR_AUTOHIDE_HEIGHT_THRESHOLD` | Additional threshold for overlap detection in pixels | `20` |

### Example

```bash
# Enable auto-hide only on monitor 0, with faster refresh
OMARCHY_BAR_AUTOHIDE_MONITORS=0 \
OMARCHY_BAR_AUTOHIDE_REFRESH_RATE=0.25 \
omarchy-bar-autohide
```

## How It Works

1. Polls Hyprland for active workspaces and window positions
2. Checks if any window overlaps the bar area
3. Monitors cursor position to reveal the bar when the cursor reaches the top pixel
4. Shows/hides by creating/removing `~/.local/state/omarchy/toggles/bar-off` — the same flag `omarchy toggle bar` uses — then nudging the shell with `omarchy-shell -q omarchy.bar syncHidden` (the shell's directory watch can miss rapid flag flips)

While the flag exists, the QML bar parks itself past the screen edge and stops
reserving workspace space (`ExclusionMode.Ignore`), so hidden really means
hidden — windows reclaim the bar's space. Revealing is just a margin change.

### Monitor Add / Lid Open Handling

When a monitor is added (e.g. opening a laptop lid while docked), Hyprland
emits `monitoradded` events and the shell takes a moment to build the bar
surface on the new output. To avoid parking a bar that does not exist yet:

- The IPC listener watches Hyprland's socket2 events.
- `monitoradded` starts a grace period where the bar stays visible.
- The first `openlayer` event ends the grace period once a layer surface is live.
- A force-resync re-applies the correct hide/show state after grace clears to
  recover from any state change made during the grace window.

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

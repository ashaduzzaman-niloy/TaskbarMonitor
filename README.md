# Taskbar Monitor

A lightweight Windows app that shows live system stats directly on your taskbar: network speed,
CPU and memory usage, CPU temperature, time since power-on, disk space and battery.

```
↑  1.2 Mbps   CPU  12%   TEMP  55°C
↓ 85   Mbps   RAM  48%   ON  3h 12m
```

Works on Windows 10 and Windows 11.

## Features

- **Always visible** — a small transparent strip that sits on the taskbar, next to the system tray
- **Network speed in Kbps / Mbps**, the same units your internet package uses, with a steady layout that doesn't jump around
- **CPU and RAM usage** with thin usage bars
- **Optional extras** — CPU temperature, time since power-on, disk space used, battery level
- **Colour warnings** — values turn amber, then red, under heavy load, high temperature or low battery
- **Adapts to your taskbar** — measures the taskbar colour and picks white or black text automatically
- **Detail panel** (left-click the strip) — CPU name, per-core usage, temperature, RAM and page file,
  network totals and local IP, free space on every drive, battery state, and the five busiest programs
- **Real power-on time** — counts from when you pressed the power button, even with Windows Fast Startup on;
  sleep, hibernation and locking don't reset it
- Hides itself over full-screen videos and games
- Read-only: the app only *reads* system information; it never changes or deletes your files

## Download

1. Go to the [**Releases**](../../releases) page and download `TaskbarMonitor.exe` from the latest release.
2. Put it anywhere you like (for example `C:\Program Files\TaskbarMonitor\`) and double-click it.
3. Approve the administrator prompt — admin rights are needed to read the CPU temperature sensor.

> **Windows SmartScreen** may say "Windows protected your PC" because the app isn't code-signed.
> Click **More info → Run anyway**. If you'd rather not run a downloaded exe, build it yourself (below) —
> it takes about five minutes.

## Using it

| Action | What happens |
|---|---|
| **Left-click** the strip | Opens the detail panel; click again, press Esc or click elsewhere to close |
| **Drag** the strip | Moves it anywhere on screen; the position is remembered (grab the text — the gaps are click-through) |
| **Right-click** the strip | Menu with the options below |

Right-click menu:

- **Show details** — opens the detail panel
- **Extra items** — add CPU temperature, time since power-on, disk space or battery
- **Taskbar text colour** — Automatic (default), White or Black
- **Detail panel theme** — Match Windows, Dark or Light
- **Temperature help…** — explains why temperature isn't showing, if it isn't
- **Restart as administrator** — needed for the temperature reading
- **Lock position / Reset position**
- **Start with Windows**
- **Exit**

### Start automatically at login

The *Start with Windows* menu option works, but Windows won't auto-start apps that need administrator
rights from the normal startup list, so temperature won't show. To start it **with** admin rights,
open PowerShell as administrator and run (change the path to where you put the exe):

```powershell
schtasks /create /tn "TaskbarMonitor" /tr "\"C:\Program Files\TaskbarMonitor\TaskbarMonitor.exe\"" /sc onlogon /rl highest
```

To remove it later: `schtasks /delete /tn "TaskbarMonitor" /f`

## Build it yourself

### 1. Requirements

- Windows 10 or 11
- [Python 3.10 or newer](https://www.python.org/downloads/) — tick **"Add python.exe to PATH"** during installation

### 2. Get the code

```powershell
git clone https://github.com/<your-username>/<repo-name>.git
cd <repo-name>
```

Or click **Code → Download ZIP** on this page and extract it.

### 3. Install the Python packages

```powershell
py -m pip install -r requirements.txt
```

### 4. Add the temperature library

CPU temperature comes from [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor):

1. Download the latest release from its
   [Releases page](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases) —
   pick the **.NET Framework 4.7.2 (net472)** build if there's a choice.
2. From the zip, copy **`LibreHardwareMonitorLib.dll`** and **`HidSharp.dll`** into this folder,
   next to `monitor.py`.
3. Make sure Windows hasn't blocked them:
   ```powershell
   Unblock-File .\LibreHardwareMonitorLib.dll, .\HidSharp.dll
   ```

Skipping this step is fine — everything else works and temperature just shows `--`.

### 5. Run it from source

```powershell
pyw monitor.py
```

Run it from a PowerShell window opened **as administrator** to get the temperature reading.
To see error messages while testing, use `py monitor.py` instead (it keeps a console window open).

### 6. Build the .exe

```powershell
py -m pip install pyinstaller
py -m PyInstaller --noconsole --onefile --name TaskbarMonitor --add-binary "LibreHardwareMonitorLib.dll;." --add-binary "HidSharp.dll;." --hidden-import clr --uac-admin monitor.py
```

The finished app is **`dist\TaskbarMonitor.exe`**. The `build` folder and `TaskbarMonitor.spec`
are temporary build files and can be deleted.

## Settings

Most settings are in the right-click menu. They're saved in `%APPDATA%\TaskbarMonitor\config.json`,
where you can also change a few extras (restart the app after editing):

| Setting | Default | What it does |
|---|---|---|
| `font` | `["Segoe UI", 9]` | Font and size of the strip |
| `interval_ms` | `1000` | How often values update, in milliseconds |
| `disk_path` | `"C:\\"` | Which drive the DISK item shows |
| `hide_on_fullscreen` | `true` | Hide the strip over full-screen apps |
| `net_exclude` | `["Loopback"]` | Network adapters to ignore — add `"vEthernet"` if Hyper-V or VPN adapters make speeds look doubled |

Delete the file to reset everything to defaults.

## Troubleshooting

**Temperature shows `--`** — right-click the strip → **Temperature help…**. The usual causes are:
the app isn't running as administrator, the two DLLs are missing or blocked, or (when running from source)
`pythonnet` isn't installed.

**`py: can't open file '...\pip'`** — use `py -m pip ...` and `py -m PyInstaller ...` (with `-m`).

**The strip covers something / is in the wrong place** — drag it, or right-click → **Reset position**.

**Speeds look doubled** — add the virtual adapter's name to `net_exclude` (see Settings).

**The strip briefly disappears when I click the taskbar** — the strip is a separate always-on-top window
over the taskbar, not part of it; it comes back within half a second.

## Project files

| File | Purpose |
|---|---|
| `monitor.py` | The app: taskbar strip, detail panel, menu, settings |
| `sensors.py` | Reads network speed, CPU temperature, power-on time and process info |
| `requirements.txt` | Python packages needed |

## Built with

- [psutil](https://github.com/giampaolo/psutil) — CPU, memory, disk, network and battery stats
- [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) — CPU temperature
- [pythonnet](https://github.com/pythonnet/pythonnet) — lets Python use the LibreHardwareMonitor library
- [PyInstaller](https://pyinstaller.org/) — packages everything into a single .exe
- tkinter — the user interface (included with Python)

The released exe bundles LibreHardwareMonitorLib and HidSharp; they remain under their own licenses,
which you can find in their repositories.

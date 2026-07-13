# DRB Edge Node — UI & Hardware Guide

The DRB Edge Node comes equipped with a highly versatile, dual-mode web interface designed to support both remote network management and local hardware integration.

This document details how to use both interfaces and how to configure physical hardware (like an LCD screen and buttons) to build a standalone, portable scanner unit.

---

## 1. Network Configuration Dashboard (`/`)

The primary dashboard is served at the root URL (e.g., `http://172.16.100.85/` or `http://localhost/`). It is a premium, responsive, glassmorphic web application designed for on-network control.

### Features
- **Live Status Monitoring**: View MQTT connectivity, Discord bot status, recording status, and the underlying OP25 core state.
- **Active Call Tracking**: When a transmission begins, the "Live Activity" card highlights to display the Talkgroup Name, System, and Talkgroup ID in real-time.
- **Local Audio Streaming**: Features an embedded HTML5 audio player hooked directly into the node's Icecast stream. **Note**: Icecast has a 2-5 second buffering delay by design, which is normal.
- **Network-Aware**: The audio stream dynamically routes to the host IP you are accessing it from (no broken `localhost` links when accessing from another PC).

---

## 2. Scanner Display Mode (`/scanner`)

This dedicated view is optimized for small physical displays (e.g., 3.5" or 5" Raspberry Pi Touchscreens). It mimics the high-contrast, information-dense layout of professional digital scanners like the Uniden SDS100.

### Features
- **High Visibility**: Large, bold typography over a pure black background.
- **Visual Call Alerts**: The channel display pulses blue when an active transmission is received.
- **Kiosk Ready**: Fixed layout with no scrolling required; perfect for running in `chromium-browser --kiosk` mode on boot.

### Hardware Button Mapping
To make the node operate like a real scanner, the UI listens for standard keyboard events. By wiring physical buttons to the Raspberry Pi's GPIO pins and using a script (like `gpiozero` or `python-uinput`) to emit keystrokes, you can build a fully functional portable unit.

| UI Action | Trigger Key | Description |
| :--- | :--- | :--- |
| **Volume Up** | `ArrowUp` | Increases the local Icecast stream volume by 10%. Shows a temporary on-screen overlay. |
| **Volume Down** | `ArrowDown` | Decreases volume by 10%. |
| **Toggle Hold** | `Enter` or `H` | Toggles the Hold state (Currently a UI mock, backend support planned). |
| **Play/Pause** | `P` | Pauses or resumes the audio stream. Useful for temporarily silencing the unit. |
| **Legend Modal** | Mouse Click | Clicking the "KEY LEGEND" button opens an on-screen modal reminding users of these mappings. |

---

## Building a Portable Unit

If you are taking the node into the field (e.g., running off a battery in the woods without internet):

1. **Hardware Requirements**:
   - Raspberry Pi (3B+ or 4 recommended).
   - Compatible SDR Dongle.
   - Small HDMI or SPI LCD Screen.
   - 3 to 5 tactile push buttons.
   - External battery pack (Ensure it can supply 3A for the Pi + SDR).

2. **Software Setup**:
   - Configure the OS to boot to desktop and auto-launch Chromium in kiosk mode pointing to `http://localhost/scanner`.
   - Run a Python script in the background that maps GPIO button presses to keyboard events (using the `keyboard` or `uinput` library) matching the table above.

3. **Offline Operation**:
   - The node will still boot and OP25 will decode the last assigned radio system, even if the MQTT broker and Discord are unreachable.
   - You can listen locally through the audio jack/speakers while watching the Scanner UI!

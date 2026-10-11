# Wi-Fi Hotspot Packet Monitor (3D Printer & IoT)

A lightweight, real-time network packet and traffic volume monitor built specifically for laptops acting as a Wi-Fi hotspot for a 3D printer (Klipper / Moonraker, OctoPrint, Bambu Lab, RepRap, etc.) and other connected devices.

---

## 🌟 Key Features

- **Preserves Core Logic & Extends Capability**:
  - Captures packets line-by-line via `tshark` in an unbuffered background daemon thread.
  - Groups packets into 60-second rollup windows in memory and flushes to SQLite on minute rollover using atomic `ON CONFLICT ... DO UPDATE` upserts.
  - Automatically purges data older than 7 days to keep disk storage low on the laptop.
- **Enhanced Filtering & Multi-Perspective Analysis**:
  - **Host IP (In & Out)**: Track total ingress and egress for your 3D printer.
  - **From Address (Source)**: Filter packets originating from a specific device.
  - **To Address (Destination)**: Filter packets destined to a specific device.
  - **Specific Pair**: Full backward compatibility with the `src -> dst` stream filter.
  - **All Traffic**: Whole-hotspot aggregate bandwidth and packet counts.
- **Dynamic Bucket Resolution**:
  - Automatically or manually select aggregation intervals: **1 Minute** (fine-grained), **5 Minutes**, **10 Minutes**, **30 Minutes**, or **1 Hour**.
- **Friendly Device Nicknames**:
  - Assign custom labels to IP addresses (e.g. `192.168.137.45` &rarr; `3D Printer (Voron 2.4)`).
- **Lightweight & High Performance**:
  - SQLite with WAL (`Write-Ahead Logging`) mode and indexes on timestamps, sources, and destinations.
  - Minimal CPU and memory usage, safe to run alongside your hotspot and slicer software.
- **Demo / Simulation Fallback**:
  - If `tshark` is not installed or when running `--mock`, the app automatically simulates realistic hotspot traffic (telemetry, G-code uploads, and web UI polling) so the UI can be tested anywhere without hardware.

---

## 🚀 Quick Start

### 1. Requirements

- Python 3.9+
- `tshark` (Part of Wireshark)
  - **Windows**:
    1. Download and run the [Wireshark Windows Installer](https://www.wireshark.org/download.html).
    2. During installation, make sure **Npcap** is checked (required for packet capture).
    3. The application automatically detects `tshark.exe` in `C:\Program Files\Wireshark\tshark.exe` or your system PATH.
  - **Debian / Ubuntu / Raspberry Pi OS**:
    ```bash
    sudo apt update
    sudo apt install tshark
    ```
  - **Arch Linux**:
    ```bash
    sudo pacman -S wireshark-cli
    ```

### 2. Installation

```bash
# In the project directory:
# On Windows PowerShell:
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# On Linux:
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Identify Your Hotspot Wi-Fi Interface

The monitor can list all network interfaces detected by `tshark`:
```bash
python app.py -L
```

#### On Windows:
When you turn on **Windows Mobile Hotspot** (Settings &rarr; Network & internet &rarr; Mobile hotspot):
- Windows creates a virtual Wi-Fi adapter (often called `Local Area Connection* X` or `Wi-Fi Direct Virtual Adapter`).
- Run `python app.py -L` to see its number (e.g. `1`, `2`, `3`) or capture on your main `"Wi-Fi"` adapter:
  ```powershell
  python app.py -i "Wi-Fi"
  # or using the interface index number from -L:
  python app.py -i 1
  ```
- If no interface is passed (`python app.py`), the app automatically searches for your active Wi-Fi or Hotspot adapter!

#### On Linux:
Check your active hotspot interface name:
```bash
ip link
# or
iw dev
```
Run with your wireless interface:
```bash
python app.py -i wlp6s0
```

### 4. Running the Monitor

```bash
# Automatic interface detection
python app.py

# Custom interface and port
python app.py -i "Wi-Fi" --port 5000

# Demo / Simulation Mode (no packet capture needed)
python app.py --mock
```

Open your browser to:
- On the host laptop: `http://localhost:5000`
- From any device on the hotspot (phone, tablet): `http://<laptop-hotspot-ip>:5000`

---

## 🔒 Running `tshark` Without Root / Sudo on Linux

By default, packet capture requires raw network socket privileges. To allow your user or script to capture packets without running `sudo python app.py`:

```bash
# Allow non-superusers to capture packets
sudo dpkg-reconfigure wireshark-common
# (Select <Yes> when asked if non-superusers should be able to capture packets)

# Add your user to the wireshark group
sudo usermod -aG wireshark $USER

# Relog or refresh your group session:
newgrp wireshark
```

Alternatively, grant capabilities directly to `dumpcap`:
```bash
sudo setcap 'CAP_NET_RAW+eip CAP_NET_ADMIN+eip' /usr/bin/dumpcap
```

---

## ⚙️ Running as a Persistent Systemd Service (Linux)

To have the monitor start automatically when your laptop boots and creates the hotspot:

Create `/etc/systemd/system/wifi-monitor.service`:
```ini
[Unit]
Description=Wi-Fi Hotspot Packet Monitor
After=network.target

[Service]
Type=simple
User=your_username
WorkingDirectory=/home/your_username/WiFi-Monitor
ExecStart=/home/your_username/WiFi-Monitor/.venv/bin/python app.py -i wlp6s0 --port 5000
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable wifi-monitor
sudo systemctl start wifi-monitor
```

---

## 📡 API Reference

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/` | `GET` | Main Dashboard Webpage |
| `/api/status` | `GET` | Capture daemon health, interface name, and database metrics |
| `/api/pairs` | `GET` | Distinct `src -> dst` pairs (preserves original reference API) |
| `/api/ips` | `GET` | All unique client IPs with packet totals and assigned nicknames |
| `/api/data` | `GET` | Aggregated timeline & breakdown stats with query filters (`mode`, `value`, `bucket`, `start`, `end`) |
| `/api/aliases` | `GET` / `POST` | Retrieve or set friendly nicknames for IP addresses |
| `/api/aliases/<ip>` | `DELETE` | Remove a device nickname |

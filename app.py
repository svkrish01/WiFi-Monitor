import os
import sys
import time
import shutil
import sqlite3
import argparse
import random
import threading
import subprocess
from urllib.parse import unquote
from flask import Flask, jsonify, request, render_template

app = Flask(__name__)
DB_NAME = os.environ.get("TRAFFIC_DB", "traffic.db")

# Global state for monitor tracking
MONITOR_STATUS = {
    "interface": "wlp6s0",
    "is_mock": False,
    "running": False,
    "last_packet_time": 0,
    "total_captured_packets": 0,
    "error_message": None
}

def get_db():
    conn = sqlite3.connect(DB_NAME, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS traffic (
                minute_epoch INTEGER,
                src_ip TEXT,
                dst_ip TEXT,
                ip_pair TEXT,
                packet_count INTEGER,
                byte_count INTEGER DEFAULT 0,
                PRIMARY KEY (minute_epoch, src_ip, dst_ip)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_traffic_epoch ON traffic(minute_epoch)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_traffic_src ON traffic(src_ip)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_traffic_dst ON traffic(dst_ip)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_traffic_pair ON traffic(ip_pair)")

        # Friendly device nicknames (e.g. 192.168.137.45 -> "3D Printer")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS device_aliases (
                ip TEXT PRIMARY KEY,
                name TEXT,
                updated_at INTEGER
            )
        """)

        # Clean up data older than 7 days (604800 seconds)
        conn.execute("DELETE FROM traffic WHERE minute_epoch < ?", (int(time.time()) - 604800,))
        conn.commit()

def cleanup_old_data_periodically():
    """Periodically trims data older than 7 days every 6 hours"""
    while True:
        time.sleep(21600)
        try:
            with get_db() as conn:
                cutoff = int(time.time()) - 604800
                conn.execute("DELETE FROM traffic WHERE minute_epoch < ?", (cutoff,))
                conn.commit()
        except Exception as e:
            print(f"[Cleanup Error] {e}", file=sys.stderr)

def flush_minute_counts(minute_epoch, minute_counts):
    """Flushes aggregated minute counts into SQLite"""
    if not minute_counts:
        return
    rows = []
    for (src, dst), stats in minute_counts.items():
        pair = f"{src} -> {dst}"
        rows.append((minute_epoch, src, dst, pair, stats['packets'], stats['bytes']))

    try:
        with get_db() as conn:
            conn.executemany("""
                INSERT INTO traffic (minute_epoch, src_ip, dst_ip, ip_pair, packet_count, byte_count)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(minute_epoch, src_ip, dst_ip) DO UPDATE SET
                    packet_count = packet_count + excluded.packet_count,
                    byte_count = byte_count + excluded.byte_count
            """, rows)
            conn.commit()
    except Exception as e:
        print(f"[DB Flush Error] {e}", file=sys.stderr)

def run_mock_traffic():
    """Generates realistic hotspot traffic for 3D printing environment when tshark is unavailable or demo mode is on"""
    MONITOR_STATUS["is_mock"] = True
    MONITOR_STATUS["running"] = True
    print("\n" + "="*65)
    print(" [*] RUNNING IN SIMULATION / DEMO MODE")
    print(" [*] Generating hotspot & 3D printer traffic (Telemetry, G-code, Web UI)")
    print("="*65 + "\n")

    # Seed initial alias defaults if empty
    with get_db() as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM device_aliases")
        if cursor.fetchone()[0] == 0:
            defaults = [
                ("192.168.137.1", "Hotspot Host (Laptop)"),
                ("192.168.137.45", "Connected Device 1"),
                ("192.168.137.89", "Connected Device 2"),
                ("192.168.137.12", "Mobile Phone"),
                ("1.1.1.1", "DNS Resolver")
            ]
            conn.executemany("INSERT OR IGNORE INTO device_aliases (ip, name, updated_at) VALUES (?, ?, ?)",
                             [(ip, name, int(time.time())) for ip, name in defaults])
            conn.commit()

        # Seed past 24 hours if database has fewer than 10 records
        cursor = conn.execute("SELECT COUNT(*) FROM traffic")
        if cursor.fetchone()[0] < 10:
            print("[*] Pre-populating historical traffic for demonstration...")
            now_epoch = int(time.time() // 60) * 60
            history_rows = []
            
            # Hotspot devices
            ips = {
                "laptop": "192.168.137.1",
                "printer": "192.168.137.45",
                "slicer": "192.168.137.89",
                "phone": "192.168.137.12",
                "dns": "1.1.1.1"
            }
            
            # Generate past 24 hours (1440 minutes)
            for m in range(1440, 0, -1):
                epoch = now_epoch - (m * 60)
                hour = (epoch // 3600) % 24

                # Base heartbeat packets
                p2l = random.randint(25, 45) # Printer -> Laptop (Moonraker status)
                l2p = random.randint(20, 35) # Laptop -> Printer

                history_rows.append((epoch, ips["printer"], ips["laptop"], f"{ips['printer']} -> {ips['laptop']}", p2l, p2l * 128))
                history_rows.append((epoch, ips["laptop"], ips["printer"], f"{ips['laptop']} -> {ips['printer']}", l2p, l2p * 84))

                # Periodic G-code print file upload burst (e.g. during day hours)
                if 8 <= hour <= 22 and (m % 180 < 15):
                    s2p = random.randint(400, 1200) # Slicer -> Printer upload
                    p2s = random.randint(150, 400)
                    history_rows.append((epoch, ips["slicer"], ips["printer"], f"{ips['slicer']} -> {ips['printer']}", s2p, s2p * 1460))
                    history_rows.append((epoch, ips["printer"], ips["slicer"], f"{ips['printer']} -> {ips['slicer']}", p2s, p2s * 96))

                # Occasional mobile phone check
                if random.random() < 0.25:
                    ph2p = random.randint(15, 60)
                    history_rows.append((epoch, ips["phone"], ips["printer"], f"{ips['phone']} -> {ips['printer']}", ph2p, ph2p * 220))
                    history_rows.append((epoch, ips["printer"], ips["phone"], f"{ips['printer']} -> {ips['phone']}", ph2p, ph2p * 840))

            conn.executemany("""
                INSERT OR IGNORE INTO traffic (minute_epoch, src_ip, dst_ip, ip_pair, packet_count, byte_count)
                VALUES (?, ?, ?, ?, ?, ?)
            """, history_rows)
            conn.commit()
            print(f"[*] Pre-populated {len(history_rows)} traffic records.")

    current_minute = int(time.time() // 60) * 60
    minute_counts = {}

    sim_pairs = [
        ("192.168.137.45", "192.168.137.1", 30, 120),    # Printer status to Host
        ("192.168.137.1", "192.168.137.45", 25, 80),     # Host ping to Printer
        ("192.168.137.89", "192.168.137.45", 15, 600),   # Slicer telemetry
        ("192.168.137.45", "192.168.137.89", 15, 200),   # Printer reply
        ("192.168.137.12", "192.168.137.45", 10, 150),   # Phone dashboard
        ("192.168.137.1", "1.1.1.1", 2, 70),              # DNS query
    ]

    while True:
        now = time.time()
        minute = int(now // 60) * 60
        MONITOR_STATUS["last_packet_time"] = now

        if minute != current_minute:
            flush_minute_counts(current_minute, minute_counts)
            minute_counts.clear()
            current_minute = minute

        # Pick random flow to tick
        src, dst, pkts_base, bytes_avg = random.choice(sim_pairs)
        pkts = random.randint(1, max(2, pkts_base // 5))
        bytes_val = pkts * bytes_avg

        key = (src, dst)
        if key not in minute_counts:
            minute_counts[key] = {'packets': 0, 'bytes': 0}
        minute_counts[key]['packets'] += pkts
        minute_counts[key]['bytes'] += bytes_val
        MONITOR_STATUS["total_captured_packets"] += pkts

        time.sleep(1.0)

def get_tshark_path():
    """Locates the tshark executable across PATH and standard Windows locations."""
    # 1. Check system PATH
    path = shutil.which("tshark")
    if path:
        return path

    # 2. Check standard Windows installations
    if os.name == 'nt':
        win_paths = [
            r"C:\Program Files\Wireshark\tshark.exe",
            r"C:\Program Files (x86)\Wireshark\tshark.exe"
        ]
        for p in win_paths:
            if os.path.isfile(p):
                return p
    return None

def get_available_interfaces(tshark_path):
    """Returns a list of available network interfaces detected by tshark."""
    if not tshark_path:
        return []
    try:
        res = subprocess.run([tshark_path, "-D"], capture_output=True, text=True, timeout=5)
        interfaces = []
        for line in res.stdout.strip().splitlines():
            line = line.strip()
            if line:
                interfaces.append(line)
        return interfaces
    except Exception:
        return []

def auto_detect_interface(tshark_path):
    """Detects a suitable default interface (Wi-Fi / Hotspot) on Windows or Linux."""
    iface_list = get_available_interfaces(tshark_path)
    if not iface_list:
        return "Wi-Fi" if os.name == 'nt' else "wlp6s0"

    # On Windows, look for Wi-Fi Direct Virtual Adapter (Hotspot) or Wi-Fi
    if os.name == 'nt':
        for item in iface_list:
            lower = item.lower()
            if "wi-fi direct" in lower or "virtual adapter" in lower:
                # Extract index number (e.g. "1. \Device\NPF...")
                idx = item.split(".")[0].strip()
                return idx
        for item in iface_list:
            lower = item.lower()
            if "wi-fi" in lower or "wireless" in lower or "802.11" in lower:
                idx = item.split(".")[0].strip()
                return idx
        return "1"  # Fallback to first interface index

    # On Linux, look for wlan or wlp
    for item in iface_list:
        lower = item.lower()
        if "wlan" in lower or "wlp" in lower:
            # Extract interface name (e.g. "1. wlan0")
            parts = item.split()
            if len(parts) >= 2:
                return parts[1]
    return "wlp6s0"

def get_interface_display_name(interface, tshark_path=None):
    """
    Returns a human-readable description of the network interface.
    E.g. On Windows for 5: 'Wi-Fi (Intel(R) Wi-Fi 6 AX201 160MHz) [Adapter #5]'
    Or on Linux for wlp6s0: 'wlp6s0'
    """
    iface_str = str(interface).strip()
    friendly_name = ""
    hardware_name = ""

    if tshark_path:
        for item in get_available_interfaces(tshark_path):
            item_clean = item.strip()
            if item_clean.startswith(f"{iface_str}.") or (f"({iface_str})" in item_clean):
                if "(" in item_clean and item_clean.endswith(")"):
                    friendly_name = item_clean[item_clean.rfind("(") + 1 : -1].strip()
                break

    if os.name == 'nt':
        try:
            res = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True, timeout=3)
            for line in res.stdout.splitlines():
                if "Description" in line and ":" in line:
                    hardware_name = line.split(":", 1)[1].strip()
                    break
        except Exception:
            pass

    if iface_str.isdigit():
        name_part = friendly_name or "Network Adapter"
        if hardware_name and ("wi-fi" in name_part.lower() or "wireless" in name_part.lower()):
            return f"{name_part} ({hardware_name}) [Adapter #{iface_str}]"
        elif hardware_name:
            return f"{name_part} [Adapter #{iface_str}] ({hardware_name})"
        else:
            return f"{name_part} [Adapter #{iface_str}]"
    else:
        if hardware_name and ("wi-fi" in iface_str.lower() or "wireless" in iface_str.lower()):
            return f"{iface_str} ({hardware_name})"
        elif friendly_name and friendly_name != iface_str:
            return f"{iface_str} ({friendly_name})"
        return iface_str

def capture_traffic(interface):
    """Runs tshark and captures packets line by line"""
    tshark_bin = get_tshark_path() or "tshark"
    cmd = [
        tshark_bin, "-l",
        "-i", str(interface),
        "-n",
        "-T", "fields",
        "-e", "frame.time_epoch",
        "-e", "ip.src",
        "-e", "ip.dst",
        "-e", "frame.len"
    ]

    print(f"[*] Starting tshark capture on interface: {interface}")
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        err = "tshark command not found in PATH or standard installation directory."
        print(f"[!] {err} Switching to demo mode.", file=sys.stderr)
        MONITOR_STATUS["error_message"] = err
        run_mock_traffic()
        return
    except Exception as e:
        err = f"Failed to start tshark: {e}"
        print(f"[!] {err} Switching to demo mode.", file=sys.stderr)
        MONITOR_STATUS["error_message"] = err
        run_mock_traffic()
        return

    MONITOR_STATUS["running"] = True
    MONITOR_STATUS["is_mock"] = False

    current_minute = 0
    minute_counts = {}
    last_flush_time = time.time()

    try:
        for line in process.stdout:
            parts = line.strip().split()
            if len(parts) < 3:
                continue

            try:
                epoch = float(parts[0])
                src_ip = parts[1]
                dst_ip = parts[2]
                frame_len = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 64

                minute = int(epoch // 60) * 60
                MONITOR_STATUS["last_packet_time"] = epoch
                MONITOR_STATUS["total_captured_packets"] += 1

                now_wall = time.time()
                # Rollover check or periodic 5-second flush for real-time UI updates
                if current_minute != 0:
                    if minute != current_minute:
                        flush_minute_counts(current_minute, minute_counts)
                        minute_counts.clear()
                        current_minute = minute
                        last_flush_time = now_wall
                    elif now_wall - last_flush_time >= 5.0 and minute_counts:
                        flush_minute_counts(current_minute, minute_counts)
                        minute_counts.clear()
                        last_flush_time = now_wall
                else:
                    current_minute = minute

                key = (src_ip, dst_ip)
                if key not in minute_counts:
                    minute_counts[key] = {'packets': 0, 'bytes': 0}
                minute_counts[key]['packets'] += 1
                minute_counts[key]['bytes'] += frame_len

            except (ValueError, IndexError):
                continue
    finally:
        # Check why tshark exited
        ret = process.poll()
        err_msg = ""
        try:
            err_msg = process.stderr.read().strip()
        except Exception:
            pass

        if err_msg:
            print(f"\n[!] tshark error output:\n    {err_msg}", file=sys.stderr)
            MONITOR_STATUS["error_message"] = err_msg
            if "permission denied" in err_msg.lower() or "dumpcap" in err_msg.lower():
                print("\n" + "="*60, file=sys.stderr)
                print(" [!] LINUX PERMISSION ISSUE DETECTED:", file=sys.stderr)
                print("     tshark cannot capture packets as your current user.", file=sys.stderr)
                print("     Run this one command on Ubuntu to fix it immediately:", file=sys.stderr)
                print("       sudo setcap 'CAP_NET_RAW+eip CAP_NET_ADMIN+eip' /usr/bin/dumpcap", file=sys.stderr)
                print("     Or run:", file=sys.stderr)
                print("       sudo usermod -aG wireshark $USER && newgrp wireshark", file=sys.stderr)
                print("="*60 + "\n", file=sys.stderr)
            elif "no such device" in err_msg.lower():
                print("\n" + "="*60, file=sys.stderr)
                print(f" [!] NETWORK INTERFACE '{interface}' NOT FOUND ON THIS SYSTEM.", file=sys.stderr)
                print("     Run 'python app.py -L' or 'ip link' to see available interfaces.", file=sys.stderr)
                print("="*60 + "\n", file=sys.stderr)

        # Final flush on shutdown
        if current_minute != 0 and minute_counts:
            flush_minute_counts(current_minute, minute_counts)
        MONITOR_STATUS["running"] = False

# ----------------- FLASK ROUTES ----------------- #

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status')
def get_status():
    with get_db() as conn:
        cursor = conn.execute("SELECT COUNT(*), MIN(minute_epoch), MAX(minute_epoch) FROM traffic")
        row = cursor.fetchone()
        db_count = row[0] or 0
        min_epoch = row[1] or 0
        max_epoch = row[2] or 0

        alias_count = conn.execute("SELECT COUNT(*) FROM device_aliases").fetchone()[0]

    return jsonify({
        "running": MONITOR_STATUS["running"],
        "interface": MONITOR_STATUS["interface"],
        "interface_display": MONITOR_STATUS.get("interface_display", MONITOR_STATUS["interface"]),
        "is_mock": MONITOR_STATUS["is_mock"],
        "error_message": MONITOR_STATUS["error_message"],
        "last_packet_time": MONITOR_STATUS["last_packet_time"],
        "session_captured_packets": MONITOR_STATUS["total_captured_packets"],
        "db_records": db_count,
        "earliest_time": min_epoch,
        "latest_time": max_epoch,
        "alias_count": alias_count
    })

@app.route('/api/pairs')
def get_pairs():
    """Preserves reference API endpoint: returns list of unique src -> dst strings"""
    with get_db() as conn:
        cursor = conn.execute("SELECT DISTINCT ip_pair FROM traffic ORDER BY ip_pair")
        return jsonify([row[0] for row in cursor.fetchall()])

@app.route('/api/ips')
def get_ips():
    """Returns all unique IPs discovered with their packet totals and aliases"""
    with get_db() as conn:
        # Load aliases
        aliases = dict(conn.execute("SELECT ip, name FROM device_aliases").fetchall())

        # Aggregate IPs
        query = """
            SELECT ip, SUM(packet_count) as total_packets, SUM(byte_count) as total_bytes,
                   MIN(minute_epoch) as first_seen, MAX(minute_epoch) as last_seen
            FROM (
                SELECT src_ip AS ip, packet_count, byte_count, minute_epoch FROM traffic
                UNION ALL
                SELECT dst_ip AS ip, packet_count, byte_count, minute_epoch FROM traffic
            )
            GROUP BY ip
            ORDER BY total_packets DESC
        """
        cursor = conn.execute(query)
        result = []
        seen_ips = set()
        for r in cursor.fetchall():
            ip = r["ip"]
            seen_ips.add(ip)
            result.append({
                "ip": ip,
                "alias": aliases.get(ip, ""),
                "display_name": f"{aliases[ip]} ({ip})" if ip in aliases and aliases[ip] else ip,
                "total_packets": r["total_packets"],
                "total_bytes": r["total_bytes"],
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"]
            })

        for ip, name in aliases.items():
            if ip not in seen_ips:
                result.append({
                    "ip": ip,
                    "alias": name,
                    "display_name": f"{name} ({ip})" if name else ip,
                    "total_packets": 0,
                    "total_bytes": 0,
                    "first_seen": None,
                    "last_seen": None
                })

        return jsonify(result)

@app.route('/api/data')
def get_data():
    """
    Flexible time-bucketed aggregation.
    Supports filtering by:
      - mode: 'all' | 'host' | 'from' | 'to' | 'pair'
      - value: IP or 'src -> dst'
      - start: epoch seconds (default now - 24 hours)
      - end: epoch seconds (default now)
      - bucket: bucket size in seconds (60, 300, 600, 1800, 3600, or 'auto')
    """
    now = int(time.time())
    start = int(request.args.get('start', now - 86400))
    end = int(request.args.get('end', now))
    mode = request.args.get('mode', 'all').strip().lower()
    value = request.args.get('value', '').strip()
    pair = request.args.get('pair', '').strip() # backwards compatibility

    # Backwards compatibility: if pair param is supplied directly
    if pair:
        mode = 'pair'
        value = pair

    # Automatically compute sensible bucket size if requested
    bucket_arg = request.args.get('bucket', 'auto').strip().lower()
    if bucket_arg == 'auto' or not bucket_arg.isdigit():
        span = end - start
        if span <= 7200:        # <= 2 hours: 1 minute
            bucket = 60
        elif span <= 43200:     # <= 12 hours: 5 minutes
            bucket = 300
        elif span <= 172800:    # <= 2 days: 10 minutes
            bucket = 600
        elif span <= 604800:    # <= 7 days: 30 minutes
            bucket = 1800
        else:
            bucket = 3600
    else:
        bucket = max(60, int(bucket_arg))

    # Construct filtering SQL
    where_clauses = ["minute_epoch >= ?", "minute_epoch <= ?"]
    params = [start, end]

    if mode == 'pair' and value:
        where_clauses.append("ip_pair = ?")
        params.append(value)
    elif mode == 'from' and value:
        where_clauses.append("src_ip = ?")
        params.append(value)
    elif mode == 'to' and value:
        where_clauses.append("dst_ip = ?")
        params.append(value)
    elif mode == 'host' and value:
        where_clauses.append("(src_ip = ? OR dst_ip = ?)")
        params.extend([value, value])

    where_sql = " AND ".join(where_clauses)

    with get_db() as conn:
        # 1. Timeline series grouped by bucket
        timeline_query = f"""
            SELECT (minute_epoch / {bucket}) * {bucket} AS bucket_epoch,
                   SUM(packet_count) AS total_packets,
                   SUM(byte_count) AS total_bytes
            FROM traffic
            WHERE {where_sql}
            GROUP BY bucket_epoch
            ORDER BY bucket_epoch ASC
        """
        cursor = conn.execute(timeline_query, params)
        timeline = [
            {
                "time": row["bucket_epoch"] * 1000,
                "count": row["total_packets"],
                "bytes": row["total_bytes"]
            }
            for row in cursor.fetchall()
        ]

        # 2. Summary stats
        summary_query = f"""
            SELECT SUM(packet_count) AS total_packets,
                   SUM(byte_count) AS total_bytes,
                   COUNT(DISTINCT ip_pair) AS active_pairs,
                   COUNT(DISTINCT src_ip) AS unique_sources,
                   COUNT(DISTINCT dst_ip) AS unique_dests
            FROM traffic
            WHERE {where_sql}
        """
        summ_row = conn.execute(summary_query, params).fetchone()
        total_packets = summ_row["total_packets"] or 0
        total_bytes = summ_row["total_bytes"] or 0

        # Calculate peak
        peak_rate = max((pt["count"] for pt in timeline), default=0)

        # 3. Top sources
        top_src_query = f"""
            SELECT src_ip, SUM(packet_count) as total_packets, SUM(byte_count) as total_bytes
            FROM traffic
            WHERE {where_sql}
            GROUP BY src_ip
            ORDER BY total_packets DESC
            LIMIT 5
        """
        top_sources = [
            {"ip": r["src_ip"], "count": r["total_packets"], "bytes": r["total_bytes"]}
            for r in conn.execute(top_src_query, params).fetchall()
        ]

        # 4. Top destinations
        top_dst_query = f"""
            SELECT dst_ip, SUM(packet_count) as total_packets, SUM(byte_count) as total_bytes
            FROM traffic
            WHERE {where_sql}
            GROUP BY dst_ip
            ORDER BY total_packets DESC
            LIMIT 5
        """
        top_dests = [
            {"ip": r["dst_ip"], "count": r["total_packets"], "bytes": r["total_bytes"]}
            for r in conn.execute(top_dst_query, params).fetchall()
        ]

        # 5. Top pairs
        top_pairs_query = f"""
            SELECT ip_pair, src_ip, dst_ip, SUM(packet_count) as total_packets, SUM(byte_count) as total_bytes
            FROM traffic
            WHERE {where_sql}
            GROUP BY ip_pair
            ORDER BY total_packets DESC
            LIMIT 10
        """
        top_pairs = [
            {
                "pair": r["ip_pair"],
                "src_ip": r["src_ip"],
                "dst_ip": r["dst_ip"],
                "count": r["total_packets"],
                "bytes": r["total_bytes"]
            }
            for r in conn.execute(top_pairs_query, params).fetchall()
        ]

        # Fetch aliases for enriching
        aliases = dict(conn.execute("SELECT ip, name FROM device_aliases").fetchall())
        for item in top_sources:
            item["alias"] = aliases.get(item["ip"], "")
        for item in top_dests:
            item["alias"] = aliases.get(item["ip"], "")
        for item in top_pairs:
            item["src_alias"] = aliases.get(item["src_ip"], "")
            item["dst_alias"] = aliases.get(item["dst_ip"], "")

    return jsonify({
        "timeline": timeline,
        "bucket_seconds": bucket,
        "filter": {
            "mode": mode,
            "value": value,
            "start": start,
            "end": end
        },
        "summary": {
            "total_packets": total_packets,
            "total_bytes": total_bytes,
            "peak_rate": peak_rate,
            "active_pairs": summ_row["active_pairs"] or 0,
            "unique_sources": summ_row["unique_sources"] or 0,
            "unique_dests": summ_row["unique_dests"] or 0
        },
        "top_sources": top_sources,
        "top_dests": top_dests,
        "top_pairs": top_pairs
    })

@app.route('/api/aliases', methods=['GET', 'POST', 'DELETE'])
def handle_aliases():
    if request.method == 'DELETE':
        with get_db() as conn:
            conn.execute("DELETE FROM device_aliases")
            conn.commit()
        return jsonify({"success": True, "message": "All device nicknames cleared"})

    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        ip = data.get('ip', '').strip()
        name = data.get('name', '').strip()
        if not ip:
            return jsonify({"error": "Missing IP"}), 400

        with get_db() as conn:
            if name:
                conn.execute("""
                    INSERT INTO device_aliases (ip, name, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(ip) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at
                """, (ip, name, int(time.time())))
            else:
                conn.execute("DELETE FROM device_aliases WHERE ip = ?", (ip,))
            conn.commit()
        return jsonify({"success": True, "ip": ip, "name": name})

    with get_db() as conn:
        cursor = conn.execute("SELECT ip, name FROM device_aliases ORDER BY ip")
        return jsonify({row[0]: row[1] for row in cursor.fetchall()})

@app.route('/api/aliases/clear', methods=['POST', 'DELETE'])
def clear_all_aliases():
    """Clears all device nicknames from the database"""
    with get_db() as conn:
        conn.execute("DELETE FROM device_aliases")
        conn.commit()
    return jsonify({"success": True, "message": "All device nicknames cleared"})

@app.route('/api/aliases/<path:ip>', methods=['DELETE'])
def delete_alias(ip):
    clean_ip = unquote(ip).strip()
    with get_db() as conn:
        conn.execute("DELETE FROM device_aliases WHERE ip = ?", (clean_ip,))
        conn.commit()
    return jsonify({"success": True, "ip": clean_ip})

@app.route('/api/devices/<path:ip>', methods=['DELETE'])
def delete_device(ip):
    """Deletes all traffic records and any assigned alias for a specific device IP"""
    clean_ip = unquote(ip).strip()
    with get_db() as conn:
        conn.execute("DELETE FROM traffic WHERE src_ip = ? OR dst_ip = ?", (clean_ip, clean_ip))
        conn.execute("DELETE FROM device_aliases WHERE ip = ?", (clean_ip,))
        conn.commit()
    return jsonify({"success": True, "message": f"Device {clean_ip} and its history removed", "ip": clean_ip})

@app.route('/api/traffic/clear', methods=['POST', 'DELETE'])
def clear_traffic():
    """Clears all traffic records from the database"""
    with get_db() as conn:
        conn.execute("DELETE FROM traffic")
        conn.commit()
    return jsonify({"success": True, "message": "All traffic history cleared"})

def main():
    tshark_path = get_tshark_path()

    parser = argparse.ArgumentParser(description="Network Traffic Monitor")
    parser.add_argument("-i", "--interface", default=None,
                        help="Network interface to capture (e.g. wlp6s0 on Linux, 'Wi-Fi' or interface index on Windows)")
    parser.add_argument("-L", "--list-interfaces", action="store_true",
                        help="List all network interfaces detectable by tshark and exit")
    parser.add_argument("-p", "--port", type=int, default=int(os.environ.get("PORT", 5000)),
                        help="Web server port (default: 5000)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Web server host binding (default: 0.0.0.0)")
    parser.add_argument("--mock", action="store_true",
                        help="Run in mock/simulation mode without tshark")
    args = parser.parse_args()

    # Handle interface listing request
    if args.list_interfaces:
        if not tshark_path:
            print("[!] tshark not found. Install Wireshark to list interfaces.", file=sys.stderr)
            sys.exit(1)
        print("\nAvailable Network Interfaces:")
        for item in get_available_interfaces(tshark_path):
            print(f"  {item}")
        print("\nUse the interface name or number with: python app.py -i <number_or_name>\n")
        sys.exit(0)

    # Determine interface
    interface = args.interface or os.environ.get("CAPTURE_INTERFACE")
    if not interface:
        interface = auto_detect_interface(tshark_path)

    MONITOR_STATUS["interface"] = str(interface)
    MONITOR_STATUS["interface_display"] = get_interface_display_name(interface, tshark_path)

    # Initialize Database
    init_db()

    # Start periodic DB cleaner
    threading.Thread(target=cleanup_old_data_periodically, daemon=True).start()

    # Start packet capture thread
    if args.mock:
        threading.Thread(target=run_mock_traffic, daemon=True).start()
    else:
        # Check if tshark exists, else fallback gracefully to mock with warning
        if tshark_path is None:
            print("[!] 'tshark' was not found in system PATH or standard folders.")
            if os.name == 'nt':
                print("    Windows Setup: Install Wireshark (https://www.wireshark.org/) and include Npcap.")
            else:
                print("    Linux Setup: sudo apt install tshark")
            print("    Falling back to simulation mode so you can view and test the dashboard.")
            threading.Thread(target=run_mock_traffic, daemon=True).start()
        else:
            threading.Thread(target=capture_traffic, args=(interface,), daemon=True).start()

    print(f"\n[*] Starting Network Traffic Monitor Web Server on http://{args.host}:{args.port}")
    print(f"[*] Accessible across network devices (e.g., http://<host-ip>:{args.port})")
    app.run(host=args.host, port=args.port, debug=False)

if __name__ == '__main__':
    main()

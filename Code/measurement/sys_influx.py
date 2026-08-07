#!/usr/bin/env python3
"""Stream general system telemetry (/proc, /sys) to InfluxDB.

Companion to influx.py / influx2.py. Where those cover CPU power and per-core
usage/freq/temp, this fills the gaps: memory, disk I/O, network I/O, and
load/system stats. Same 1s cadence, same bucket/server-tag convention.

Pure stdlib — every source file here is world-readable, so unlike RAPL this
does NOT need root. Counter-based metrics (disk, net, context switches) are
emitted as rates using the same prev-sample delta pattern influx2.py uses for
the RAPL energy counter.

Run:        python3 sys_influx.py
Self-check: python3 sys_influx.py --selfcheck
"""

import re
import sys
import time
import socket
import argparse
from pathlib import Path

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

TOKEN_PATH = Path.home() / ".secrets" / "influx_token.txt"
ORG_PATH = Path.home() / ".secrets" / "influx_org.txt"

INFLUX_URL = "http://localhost:8086"
INFLUX_BUCKET = "Power"
SAMPLE_INTERVAL_S = 1.0

# Whole block devices only — skip partitions, loop, ram, dm. Tune as needed.
DISK_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|hd[a-z]+)$")
# Network interfaces to skip (loopback is noise for this dashboard).
NET_SKIP = {"lo"}

SECTOR_BYTES = 512
KB = 1024


def load_secret(path):
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        print(f"Error: file not found at {path}.\n"
              "Create it with your InfluxDB value, e.g.:\n"
              f"  mkdir -p {path.parent} && echo '<value>' > {path}",
              file=sys.stderr)
        sys.exit(1)
    except PermissionError:
        print(f"Error: permission denied reading {path} (chmod 600 it).",
              file=sys.stderr)
        sys.exit(1)


# --- /proc parsers: each returns plain dicts so they're testable offline ---

def parse_meminfo(text):
    """meminfo -> bytes. Fields are reported in kB."""
    vals = {}
    for line in text.splitlines():
        k, _, rest = line.partition(":")
        vals[k] = int(rest.split()[0]) * KB  # all lines we use are in kB
    total = vals["MemTotal"]
    avail = vals.get("MemAvailable", vals["MemFree"])
    return {
        "total": total,
        "free": vals["MemFree"],
        "available": avail,
        "used": total - avail,
        "cached": vals.get("Cached", 0),
        "swap_total": vals.get("SwapTotal", 0),
        "swap_used": vals.get("SwapTotal", 0) - vals.get("SwapFree", 0),
    }


def parse_diskstats(text):
    """diskstats -> {device: (reads, sectors_read, writes, sectors_written)}."""
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) < 10:
            continue
        name = f[2]
        if not DISK_RE.match(name):
            continue
        out[name] = (int(f[3]), int(f[5]), int(f[7]), int(f[9]))
    return out


def parse_netdev(text):
    """net/dev -> {iface: (rx_bytes, rx_packets, tx_bytes, tx_packets)}."""
    out = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        name = name.strip()
        if name in NET_SKIP:
            continue
        f = rest.split()
        if len(f) < 16:
            continue
        out[name] = (int(f[0]), int(f[1]), int(f[8]), int(f[9]))
    return out


def parse_system(loadavg, stat, uptime):
    """loadavg/stat/uptime -> dict; ctxt is a raw counter (rated later)."""
    la = loadavg.split()
    procs_running = 0
    ctxt = 0
    for line in stat.splitlines():
        if line.startswith("ctxt "):
            ctxt = int(line.split()[1])
        elif line.startswith("procs_running "):
            procs_running = int(line.split()[1])
    return {
        "load1": float(la[0]),
        "load5": float(la[1]),
        "load15": float(la[2]),
        "procs_running": procs_running,
        "uptime_s": float(uptime.split()[0]),
        "ctxt": ctxt,  # counter, converted to ctxt_per_s on write
    }


def rate(curr, prev, dt):
    """Per-second rate; counter reset / wrap -> None (skip this cycle)."""
    d = curr - prev
    return None if d < 0 else d / dt


def read(path):
    with open(path) as fh:
        return fh.read()


def collect_points(server, prev, dt):
    """Read /proc, return (points, new_prev_counters)."""
    points = []
    new_prev = {}

    mem = parse_meminfo(read("/proc/meminfo"))
    p = Point("mem").tag("server", server)
    for k, v in mem.items():
        p.field(k, int(v))
    points.append(p)

    disks = parse_diskstats(read("/proc/diskstats"))
    new_prev["disk"] = disks
    for dev, cur in disks.items():
        old = prev.get("disk", {}).get(dev)
        if not old:
            continue
        rb, ri = rate(cur[1], old[1], dt), rate(cur[0], old[0], dt)
        wb, wi = rate(cur[3], old[3], dt), rate(cur[2], old[2], dt)
        if None in (rb, ri, wb, wi):
            continue
        points.append(
            Point("disk_io").tag("server", server).tag("device", dev)
            .field("read_bps", rb * SECTOR_BYTES).field("write_bps", wb * SECTOR_BYTES)
            .field("read_iops", ri).field("write_iops", wi)
        )

    nets = parse_netdev(read("/proc/net/dev"))
    new_prev["net"] = nets
    for iface, cur in nets.items():
        old = prev.get("net", {}).get(iface)
        if not old:
            continue
        rxb, rxp = rate(cur[0], old[0], dt), rate(cur[1], old[1], dt)
        txb, txp = rate(cur[2], old[2], dt), rate(cur[3], old[3], dt)
        if None in (rxb, rxp, txb, txp):
            continue
        points.append(
            Point("net_io").tag("server", server).tag("iface", iface)
            .field("rx_bps", rxb).field("tx_bps", txb)
            .field("rx_pps", rxp).field("tx_pps", txp)
        )

    sysm = parse_system(read("/proc/loadavg"), read("/proc/stat"), read("/proc/uptime"))
    new_prev["ctxt"] = sysm["ctxt"]
    sp = (Point("system").tag("server", server)
          .field("load1", sysm["load1"]).field("load5", sysm["load5"])
          .field("load15", sysm["load15"])
          .field("procs_running", sysm["procs_running"])
          .field("uptime_s", sysm["uptime_s"]))
    cps = rate(sysm["ctxt"], prev.get("ctxt", sysm["ctxt"]), dt)
    if cps is not None:
        sp.field("ctxt_per_s", cps)
    points.append(sp)

    return points, new_prev


def main():
    ap = argparse.ArgumentParser(description="Stream /proc system telemetry to InfluxDB.")
    ap.add_argument("--server", default=socket.gethostname(),
                    help="Server tag (default: hostname).")
    ap.add_argument("--selfcheck", action="store_true",
                    help="Run offline parser/rate self-test and exit.")
    args = ap.parse_args()

    if args.selfcheck:
        return selfcheck()

    token = load_secret(TOKEN_PATH)
    org = load_secret(ORG_PATH)

    client = InfluxDBClient(url=INFLUX_URL, token=token, org=org)
    write_api = client.write_api(write_options=SYNCHRONOUS)
    print(f"[sys_influx] server={args.server} interval={SAMPLE_INTERVAL_S}s "
          f"-> {INFLUX_URL} bucket={INFLUX_BUCKET}. Ctrl+C to stop.")

    prev = {}
    last = time.perf_counter()
    deadline = last + SAMPLE_INTERVAL_S
    # Prime counters so the first emitted cycle has real deltas.
    _, prev = collect_points(args.server, prev, 1.0)

    try:
        while True:
            now = time.perf_counter()
            if deadline > now:
                time.sleep(deadline - now)
            else:
                deadline = now
            t = time.perf_counter()
            dt = t - last
            points, prev = collect_points(args.server, prev, dt)
            try:
                write_api.write(bucket=INFLUX_BUCKET, org=org, record=points)
                print(f"Sent {len(points)} points (dt={dt:.2f}s)")
            except Exception as e:
                print(f"[{time.ctime()}] write error: {e}", file=sys.stderr)
            last = t
            deadline += SAMPLE_INTERVAL_S
    except KeyboardInterrupt:
        print("\n[sys_influx] stopped.")
    finally:
        write_api.close()
        client.close()


def selfcheck():
    mem = parse_meminfo("MemTotal: 100 kB\nMemFree: 40 kB\n"
                        "MemAvailable: 60 kB\nCached: 10 kB\n"
                        "SwapTotal: 8 kB\nSwapFree: 6 kB\n")
    assert mem["total"] == 100 * KB and mem["used"] == 40 * KB
    assert mem["swap_used"] == 2 * KB

    ds = parse_diskstats(
        " 259 0 nvme0n1 10 0 200 0 5 0 100 0 0 0 0 0 0 0\n"
        "   7 0 loop0 1 0 1 0 0 0 0 0 0 0 0 0 0 0\n")
    assert set(ds) == {"nvme0n1"}, ds  # partitions/loop excluded
    assert ds["nvme0n1"] == (10, 200, 5, 100)

    nd = parse_netdev("Inter-|\n face |\n"
                      "  eth0: 1000 10 0 0 0 0 0 0 500 5 0 0 0 0 0 0\n"
                      "    lo: 9 9 0 0 0 0 0 0 9 9 0 0 0 0 0 0\n")
    assert set(nd) == {"eth0"}, nd  # lo skipped
    assert nd["eth0"] == (1000, 10, 500, 5)

    sysm = parse_system("0.5 1.0 1.5 2/300 1234",
                        "ctxt 1000\nprocs_running 3\n", "123.45 600.0")
    assert sysm["load1"] == 0.5 and sysm["procs_running"] == 3
    assert sysm["ctxt"] == 1000 and sysm["uptime_s"] == 123.45

    # rate math + reset handling
    assert rate(200, 100, 2.0) == 50.0
    assert rate(5, 100, 1.0) is None  # counter reset -> skip
    print("selfcheck OK")


if __name__ == "__main__":
    main()

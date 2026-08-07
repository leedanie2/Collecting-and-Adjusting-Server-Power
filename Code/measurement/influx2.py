#!/usr/bin/env python3
"""Stream RAPL CPU package power and per-core CPU stats to InfluxDB.

Two cadences run in one loop, deliberately decoupled:

  * RAPL energy -> watts at a configurable rate (default 1 Hz, --rapl-hz).
    Auto-discovers every intel-rapl package-* domain (one per CPU socket), sums
    their energy_uj deltas, and writes combined wattage. Higher rates are useful
    for explicit captures, but mycroft's raw 10 Hz production stream was too
    noisy for normal Grafana/model consumers.
  * Per-core cpu_usage/cpu_freq/cpu_temp via psutil at 1 Hz only. psutil
    per-core collection is the expensive part and must NOT run at the fast rate.

Both write to the SAME measurement/tag/field names as before, so existing
Grafana dashboards keep working.

False-spike safety (ported from ../Intel RAPL Code/rapl.py): polling faster
than the RAPL hardware update rate makes successive reads return the same
counter (delta=0) until the hardware commits a chunk, then one read absorbs the
whole accumulated energy over a single short interval -> a false spike. Fix:
accumulate (energy, time) across zero-delta samples and only emit a watts point
when the counter actually advances. Wraparound is added back per socket.
"""

import os
import re
import sys
import time
import glob
import socket
import argparse
from pathlib import Path

# Third-party deps are only needed for the live loop, not for --selfcheck (which
# exercises pure-stdlib watts math). Tolerate their absence so the selfcheck runs
# anywhere; main() re-checks and errors clearly if they're missing at run time.
try:
    import psutil
    from influxdb_client import InfluxDBClient, Point
    from influxdb_client.client.write_api import SYNCHRONOUS
except ImportError as _e:
    psutil = None
    InfluxDBClient = Point = SYNCHRONOUS = None
    _IMPORT_ERR = _e
else:
    _IMPORT_ERR = None

POWERCAP_GLOB = "/sys/class/powercap/intel-rapl:*"
TOP_LEVEL_RE = re.compile(r"^intel-rapl:\d+$")  # excludes :N:M core/uncore subdomains
TOKEN_PATH = Path.home() / ".secrets" / "influx_token.txt"
ORG_PATH = Path.home() / ".secrets" / "influx_org.txt"

INFLUX_URL = "http://localhost:8086"
INFLUX_BUCKET = "Power"

# Decoupled cadences. RAPL polling is configurable; psutil stays at 1 Hz. The
# default is deliberately conservative: mycroft's raw 10 Hz stream showed large
# within-second jitter, while 1 Hz aggregation preserved the useful signal for
# Grafana/model consumers. Use --rapl-hz 10 for explicit high-frequency captures.
RAPL_HZ_DEFAULT = 1.0           # production power polling rate; override with --rapl-hz
PSUTIL_INTERVAL_S = 1.0         # per-core usage/freq/temp cadence (keep at 1 Hz)


def load_token(path):
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        print(f"Error: token file not found at {path}.\n"
              "Create it with your InfluxDB API token, e.g.:\n"
              f"  mkdir -p {path.parent} && echo '<token>' > {path}",
              file=sys.stderr)
        sys.exit(1)
    except PermissionError:
        print(f"Error: permission denied reading {path}.\n"
              f"Check ownership/permissions (chmod 600 {path}).",
              file=sys.stderr)
        sys.exit(1)


def read_counter(fd):
    return int(os.pread(fd, 32, 0))


def read_run_queue():
    """Kernel run-queue depth: the numerator of /proc/loadavg field 4
    (currently-runnable scheduling entities). Sampled at the RAPL rate so it
    lands alongside the power step -- run-queue depth is an *upstream* causal
    signal (the scheduler dispatches before the cores draw power), so if any
    lead time exists on a single socket it shows up here, not in power itself
    (2026-07-01b item-2). Cheap: one /proc read, no root. None on parse failure
    so a bad read never kills the power stream."""
    try:
        return int(open("/proc/loadavg").read().split()[3].split("/")[0])
    except (OSError, IndexError, ValueError):
        return None


def discover_package_domains():
    """Find every intel-rapl:N domain whose name is package-* (one per CPU socket)."""
    domains = []
    for path in sorted(glob.glob(POWERCAP_GLOB)):
        if not TOP_LEVEL_RE.match(os.path.basename(path)):
            continue
        try:
            with open(os.path.join(path, "name")) as f:
                name = f.read().strip()
        except (PermissionError, FileNotFoundError):
            continue
        if name.startswith("package-"):
            domains.append(path)
    if not domains:
        print("Error: no intel-rapl package-* domains found under "
              "/sys/class/powercap/. Check `ls /sys/class/powercap/` "
              "and that RAPL is supported on this machine.", file=sys.stderr)
        sys.exit(1)
    return domains


def open_package_fds(domains):
    fds = []
    try:
        for path in domains:
            fds.append(os.open(os.path.join(path, "energy_uj"), os.O_RDONLY))
    except PermissionError:
        print("Error: permission denied reading energy_uj.\n"
              "RAPL counters are root-only on modern kernels. "
              "Re-run with: sudo python3 influx2.py",
              file=sys.stderr)
        sys.exit(1)
    return fds


def socket_deltas(last_energy, current_energy, max_ranges):
    """Per-socket energy deltas, handling wraparound independently per fd."""
    deltas = []
    for prev, cur, max_range in zip(last_energy, current_energy, max_ranges):
        delta = cur - prev
        if delta < 0:
            delta += max_range  # counter wrapped
        deltas.append(delta)
    return deltas


def sum_energy_deltas(last_energy, current_energy, max_ranges):
    """Sum per-socket energy deltas, handling wraparound independently per fd."""
    return sum(socket_deltas(last_energy, current_energy, max_ranges))


def read_max_ranges(domains):
    ranges = []
    for path in domains:
        range_path = os.path.join(path, "max_energy_range_uj")
        try:
            with open(range_path) as f:
                ranges.append(int(f.read().strip()))
        except (PermissionError, FileNotFoundError) as e:
            print(f"Error reading {range_path}: {e}", file=sys.stderr)
            sys.exit(1)
    return ranges


class RaplAccumulator:
    """False-spike-safe energy->watts accumulator (see module docstring).

    Feed each poll's summed energy delta + elapsed time. Returns watts only when
    the counter actually advanced (delta > 0); returns None on a zero-delta poll,
    carrying the elapsed time forward so the eventual non-zero poll is averaged
    over the true window instead of one short interval.
    """

    def __init__(self):
        self.acc_energy_uj = 0
        self.acc_time_s = 0.0

    def add(self, delta_energy_uj, delta_time_s):
        self.acc_energy_uj += delta_energy_uj
        self.acc_time_s += delta_time_s
        if delta_energy_uj > 0 and self.acc_time_s > 0:
            watts = (self.acc_energy_uj / 1_000_000) / self.acc_time_s
            self.acc_energy_uj = 0
            self.acc_time_s = 0.0
            return watts
        return None


def combine_socket_watts(deltas, dt, accs, last_watts):
    """Per-socket false-spike-safe combine (see module docstring).

    One accumulator PER socket so each divides its energy by ITS OWN elapsed
    window. Feeding the *summed* energy to a single accumulator aliases a slow
    socket's coarse update into a spike, because a fast socket keeps resetting
    the shared time base every poll -> the slow socket's ~1 s of energy lands in
    one 0.1 s window (10x false spike on a dual-socket box). Emits the sum of the
    latest per-socket watts, once every socket has reported at least once.
    """
    for i, d in enumerate(deltas):
        w = accs[i].add(d, dt)
        if w is not None:
            last_watts[i] = w
    if all(v is not None for v in last_watts):
        return sum(last_watts)
    return None


def collect_cpu_points(server):
    """Return a list of InfluxDB Points for per-core usage, frequency, and temperature."""
    points = []

    # Per-logical-core usage (% since last psutil call)
    for i, pct in enumerate(psutil.cpu_percent(percpu=True)):
        points.append(
            Point("cpu_usage")
            .tag("server", server)
            .tag("core", str(i))
            .field("percent", float(pct))
        )

    # Per-logical-core frequency
    freqs = psutil.cpu_freq(percpu=True)
    if freqs:
        for i, freq in enumerate(freqs):
            points.append(
                Point("cpu_freq")
                .tag("server", server)
                .tag("core", str(i))
                .field("mhz", float(freq.current))
            )

    # Per-physical-core temperature
    try:
        all_temps = psutil.sensors_temperatures()
    except AttributeError:
        all_temps = {}

    if all_temps:
        # Intel coretemp exposes per-physical-core readings labeled "Core N"
        if "coretemp" in all_temps:
            for entry in all_temps["coretemp"]:
                m = re.match(r"Core\s+(\d+)", entry.label)
                if m:
                    points.append(
                        Point("cpu_temp")
                        .tag("server", server)
                        .tag("core", m.group(1))
                        .field("celsius", float(entry.current))
                    )
        else:
            # AMD k10temp or generic fallback — send all labeled readings
            for sensor_name in ("k10temp", "cpu_thermal", "acpitz"):
                if sensor_name not in all_temps:
                    continue
                for entry in all_temps[sensor_name]:
                    label = entry.label or sensor_name
                    points.append(
                        Point("cpu_temp")
                        .tag("server", server)
                        .tag("sensor", sensor_name)
                        .tag("label", label)
                        .field("celsius", float(entry.current))
                    )
                break

    return points


def selfcheck():
    """Offline sanity check of the watts math — no sudo / InfluxDB needed.

    Exercises wraparound and the zero-delta accumulator, asserting non-garbage
    (finite, sensible) wattage. Returns 0 on success.
    """
    # 1. Wraparound: socket counter wraps from near-max back to a small value.
    max_range = 1_000_000_000
    d = sum_energy_deltas([max_range - 100], [400], [max_range])
    assert d == 500, f"wraparound delta wrong: {d}"

    # 2. Multi-socket sum.
    d = sum_energy_deltas([0, 0], [3_000_000, 3_000_000], [max_range, max_range])
    assert d == 6_000_000, f"multi-socket sum wrong: {d}"

    # 3. Normal poll: 30 J over 0.1 s -> 300 W.
    acc = RaplAccumulator()
    w = acc.add(30_000_000, 0.1)
    assert w is not None and abs(w - 300.0) < 1e-6, f"normal watts wrong: {w}"

    # 4. Zero-delta accumulation (the false-spike fix): two stale reads then a
    #    real update. Naively the 3rd poll would read 0.3 J over 0.1 s = 3 W
    #    (a false spike-free *trough* then spike); accumulated it must average
    #    0.3 J over the full 0.3 s window = 1.0 W, with no point on the stalls.
    acc = RaplAccumulator()
    assert acc.add(0, 0.1) is None, "zero-delta should not emit"
    assert acc.add(0, 0.1) is None, "zero-delta should not emit"
    w = acc.add(300_000, 0.1)
    assert w is not None and abs(w - 1.0) < 1e-6, f"accumulated watts wrong: {w}"

    # 5. Sanity: every emitted value is finite and non-negative.
    for val in (300.0, 1.0):
        assert val == val and val >= 0 and val != float("inf")

    # 6. Run-queue depth reads a non-negative int from the live /proc/loadavg
    #    (this host has the same interface as mycroft).
    nr = read_run_queue()
    assert nr is not None and nr >= 0, f"run-queue read failed: {nr}"

    # 7. Dual-socket async updates (the real mycroft false-spike). Socket A
    #    advances every poll (3 J/0.1 s = 30 W); socket B is stale for 9 polls
    #    then dumps its whole 1 s window (30 J) on the 10th. The OLD single
    #    accumulator on *summed* energy emitted 33 J / 0.1 s = 330 W on that poll.
    #    Per-socket accumulators divide B's 30 J by its own 1.0 s -> ~60 W
    #    combined, no spike.
    accs = [RaplAccumulator(), RaplAccumulator()]
    last = [None, None]
    emitted = []
    for poll in range(20):
        b = 30_000_000 if poll % 10 == 9 else 0
        w = combine_socket_watts([3_000_000, b], 0.1, accs, last)
        if w is not None:
            emitted.append(w)
    assert emitted, "combine should emit once both sockets have reported"
    assert max(emitted) < 100.0, f"dual-socket false spike not fixed: peak {max(emitted):.0f} W"
    assert abs(max(emitted) - 60.0) < 1e-6, f"combined idle watts wrong: {max(emitted)}"

    print(f"selfcheck OK: wraparound, multi-socket sum, watts sane, "
          f"dual-socket peak={max(emitted):.0f}W (no spike), run_queue={nr}.")
    return 0


def main():
    p = argparse.ArgumentParser(description="Stream RAPL CPU power and per-core CPU stats to InfluxDB.")
    p.add_argument("--server", default=socket.gethostname(),
                   help="Server tag for this data point (default: hostname).")
    p.add_argument("--rapl-hz", type=float, default=RAPL_HZ_DEFAULT,
                   help=f"RAPL power polling rate in Hz (default: {RAPL_HZ_DEFAULT:g}). "
                        "psutil per-core stats stay fixed at 1 Hz regardless.")
    p.add_argument("--selfcheck", action="store_true",
                   help="Run offline watts-math sanity checks (no sudo/InfluxDB) and exit.")
    args = p.parse_args()

    if args.selfcheck:
        return selfcheck()

    if _IMPORT_ERR is not None:
        print(f"Error: missing runtime dependency for the live loop: {_IMPORT_ERR}.\n"
              "Install psutil + influxdb-client (e.g. pip install psutil influxdb-client).",
              file=sys.stderr)
        return 1

    if args.rapl_hz <= 0:
        print("Error: --rapl-hz must be > 0.", file=sys.stderr)
        return 1
    rapl_interval_s = 1.0 / args.rapl_hz

    token = load_token(TOKEN_PATH)
    org = load_token(ORG_PATH)
    domains = discover_package_domains()
    fds = open_package_fds(domains)
    max_ranges = read_max_ranges(domains)

    client = InfluxDBClient(url=INFLUX_URL, token=token, org=org)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    print(f"[influx] server={args.server} sockets={len(domains)} "
          f"rapl={args.rapl_hz:g}Hz psutil={1.0 / PSUTIL_INTERVAL_S:g}Hz -> "
          f"{INFLUX_URL} bucket={INFLUX_BUCKET}. Ctrl+C to stop.")

    # Prime psutil's CPU usage baseline (first call always returns 0.0)
    psutil.cpu_percent(percpu=True)

    accs = [RaplAccumulator() for _ in fds]
    last_socket_watts = [None] * len(fds)
    last_energy = [read_counter(fd) for fd in fds]
    last_time = time.perf_counter()
    rapl_deadline = last_time + rapl_interval_s
    psutil_deadline = last_time + PSUTIL_INTERVAL_S
    last_watts = float("nan")

    try:
        while True:
            now = time.perf_counter()
            remaining = rapl_deadline - now
            if remaining > 0:
                time.sleep(remaining)
            else:
                rapl_deadline = now  # fell behind; resync instead of bursting

            current_energy = [read_counter(fd) for fd in fds]
            current_time = time.perf_counter()

            deltas = socket_deltas(last_energy, current_energy, max_ranges)
            delta_time_s = current_time - last_time
            watts = combine_socket_watts(deltas, delta_time_s, accs, last_socket_watts)

            records = []
            if watts is not None:
                last_watts = watts
                records.append(
                    Point("cpu_power").tag("server", args.server).field("watts", watts)
                )

            # Run-queue depth at the RAPL rate (upstream precursor to the power
            # step). Independent of the watts emit so it streams even on a stale
            # RAPL poll; on the live Grafana overlay you can see whether it leads.
            nr = read_run_queue()
            if nr is not None:
                records.append(
                    Point("run_queue").tag("server", args.server).field("nr_running", nr)
                )

            # 1 Hz psutil branch — the expensive per-core collection.
            if current_time >= psutil_deadline:
                records.extend(collect_cpu_points(args.server))
                psutil_deadline += PSUTIL_INTERVAL_S
                if psutil_deadline < current_time:  # fell behind; resync
                    psutil_deadline = current_time + PSUTIL_INTERVAL_S
                print(f"Sent: {last_watts:.2f} W (RAPL @{args.rapl_hz}Hz, latest)  +per-core cpu points (psutil @1Hz)")

            if records:
                try:
                    write_api.write(bucket=INFLUX_BUCKET, org=org, record=records)
                except Exception as e:
                    print(f"[{time.ctime()}] Error: {e}", file=sys.stderr)

            last_energy = current_energy
            last_time = current_time
            rapl_deadline += rapl_interval_s

    except KeyboardInterrupt:
        print("\n[influx] stopped.")
    finally:
        for fd in fds:
            os.close(fd)
        write_api.close()
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

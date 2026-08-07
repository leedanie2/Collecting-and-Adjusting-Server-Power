#!/usr/bin/env python3
"""
collect_rapl.py - CPU package power logger via Linux powercap/hwmon.
Writes 'time_s,power_W' rows to stdout at 100 ms intervals.

Usage:
    sudo python3 collect_rapl.py > rapl_data.csv
    # Ctrl-C to stop.

Supports Intel (powercap intel-rapl) and AMD (hwmon power1_input).
Root is required on most systems; or lower perf_event_paranoid:
    echo 0 | sudo tee /proc/sys/kernel/perf_event_paranoid
"""
import sys, time, os, glob, re

INTERVAL = 0.1  # seconds between samples
TOP_LEVEL_RE = re.compile(r'^intel-rapl:\d+$')  # excludes :N:M core/uncore subdomains


def find_package_domains():
    """Every intel-rapl:N domain whose name is package-* (one per CPU socket).
    Ported from ../Grafana/influx.py — domain indices aren't assumed (e.g.
    intel-rapl:1 is sometimes the psys platform domain, not a second package),
    and single-socket collect_rapl.py silently missed the second package."""
    domains = []
    for path in sorted(glob.glob('/sys/class/powercap/intel-rapl:*')):
        if not TOP_LEVEL_RE.match(os.path.basename(path)):
            continue
        try:
            with open(os.path.join(path, 'name')) as f:
                name = f.read().strip()
        except (PermissionError, FileNotFoundError):
            continue
        if name.startswith('package-'):
            domains.append(os.path.join(path, 'energy_uj'))
    return domains


def find_energy_source():
    domains = find_package_domains()
    if domains:
        max_paths = [os.path.join(os.path.dirname(p), 'max_energy_range_uj') for p in domains]
        max_ranges = [ruj(p) if os.path.exists(p) else None for p in max_paths]
        return 'intel', domains, max_ranges

    mmio = '/sys/class/powercap/intel-rapl-mmio:0/energy_uj'
    if os.path.exists(mmio):
        max_path = os.path.join(os.path.dirname(mmio), 'max_energy_range_uj')
        return 'intel', [mmio], [ruj(max_path) if os.path.exists(max_path) else None]

    for path in sorted(glob.glob('/sys/class/hwmon/hwmon*/power1_input')):
        return 'amd', [path], [None]

    sys.exit(
        'ERROR: No power interface found.\n'
        'Intel: sudo modprobe intel_rapl_common\n'
        'AMD:   check /sys/class/hwmon/\n'
        'Alt:   sudo perf stat -e power/energy-pkg/ -I 100 -- <benchmark>'
    )


def ruj(path):
    with open(path) as f:
        return int(f.read())


def sum_deltas(prev_vals, cur_vals, max_ranges):
    total = 0
    for prev, cur, max_range in zip(prev_vals, cur_vals, max_ranges):
        delta = cur - prev
        if delta < 0 and max_range:   # counter wraparound (~every few hundred J)
            delta += max_range
        total += delta
    return total


def main():
    kind, paths, max_ranges = find_energy_source()

    print('time_s,power_W', flush=True)
    t0 = time.monotonic()

    if kind == 'amd':
        # AMD hwmon reports instantaneous power in microwatts
        while True:
            t = time.monotonic() - t0
            print(f'{t:.3f},{ruj(paths[0]) / 1e6:.3f}', flush=True)
            time.sleep(INTERVAL)
    else:
        # Intel RAPL: energy counters (uJ) summed across sockets, differentiated to get power
        e0 = [ruj(p) for p in paths]
        t_prev = time.monotonic()
        time.sleep(INTERVAL)
        while True:
            # Read the counters, then stamp. Marginally tighter than stamping
            # first, but it does not change any measurement.
            #
            # RESOLVED 2026-07-28. aisim2 traces carry ~60 isolated single-sample
            # dips near 100 W, and 10 samples across the set exceed even PL2
            # (492 W), peaking at 553 W. Both are the SAME artifact seen from
            # opposite ends: the energy counter does not advance on every read,
            # so one interval under-reports and the next dumps the accumulated
            # energy. Evidence:
            #   * every >PL2 spike is preceded by an unusually low sample, and
            #     (spike+prev)/2 lands back on the local level (median 1.02);
            #   * averaging adjacent samples removes ALL 52 dips in
            #     run1/aisim2_baseline (min 100.8 W -> 150.4 W);
            #   * dt is a clean 0.100 s at every dip -- the timing is fine, it
            #     is the ENERGY that is misattributed between neighbours.
            # The earlier "cannot be real, bare idle is 198.5 W" reasoning was
            # right that the samples are not physical, and the earlier guess
            # that they were C-state excursions was wrong.
            #
            # Impact on results: negligible. The artifact lives at 0.1 s and
            # every grid metric is computed after the 15 s UPS low-pass, which
            # removes it -- UPS-filtered CV moves 0.1223 -> 0.1214 and trace
            # energy by 0.06%. Do NOT despike the committed traces for that
            # 0.7%: it would change the instrument mid-study for no gain.
            #
            # For FUTURE collections, rapl.odt describes the correct fix (emit
            # only when the counter advanced, dividing by time since the last
            # advance). It is documented there but NOT implemented below.
            e1 = [ruj(p) for p in paths]
            t1 = time.monotonic()
            dt = t1 - t_prev
            de = sum_deltas(e0, e1, max_ranges)
            print(f'{t1 - t0:.3f},{de / 1e6 / dt:.3f}', flush=True)
            e0, t_prev = e1, t1
            time.sleep(INTERVAL)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass

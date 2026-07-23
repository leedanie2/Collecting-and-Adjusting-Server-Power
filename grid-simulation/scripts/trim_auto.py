#!/usr/bin/env python3
"""Trim a ramp.c trace to its real-workload window, detecting the offsets.

trim_rampc.py needs hand-supplied offsets read out of ramp.c's stderr tick log.
recollect.sh doesn't capture that log, and the sequential 124-core ramp settles
at its own pace per workload (~80 s up, 40-90 s down observed), so the offsets
can't be assumed either. Detect them from the power trace instead.

ramp.c holds its ballast at 100% for the whole workload, so the real-workload
window is the high plateau: everything between the FIRST and LAST crossing of a
threshold set just under the plateau level. First/last crossing rather than a
contiguous run, because ai_sim_2 dips to idle by design mid-workload and a
contiguous-run detector would stop at the first dip.

    trim_auto.py <in.csv> [out.csv]     # out defaults to stdout-safe report only
    trim_auto.py --selfcheck
"""
import csv, sys


def load(path):
    rows = []
    for a, b in list(csv.reader(open(path)))[1:]:
        if a and b:
            rows.append((float(a), float(b)))
    return rows


def detect_window(rows, frac=0.99):
    """Return (t_start, t_end) of the real-workload plateau.

    plateau = median of the upper half of samples (robust to the ramp legs and
    to mid-workload idle dips); threshold = frac * plateau.

    Known bias: the threshold sits (1-frac)*plateau BELOW the plateau, and the
    ramp leg crosses it slightly before the plateau truly starts, so the window
    is a mild OVER-estimate -- about (1-frac)*plateau/ramp_slope seconds of ramp
    leg at each end (~1.6 s per end at frac=0.99 on a 2.5 W/s ramp). That is
    deliberate: erring toward including a sliver of ramp is safer than clipping
    real workload, and it is negligible against the ~80 s of ballast padding
    this removes. Do not read the window edges as exact ramp boundaries.
    """
    ps = sorted(p for _, p in rows)
    upper = ps[len(ps) // 2:]
    plateau = upper[len(upper) // 2]
    thr = frac * plateau
    hits = [t for t, p in rows if p >= thr]
    if len(hits) < 2:
        raise SystemExit("no plateau found -- is this a ramp.c trace?")
    return hits[0], hits[-1], plateau, thr


def trim(rows, t0, t1):
    kept = [(t, p) for t, p in rows if t0 <= t <= t1]
    z = kept[0][0]
    return [(round(t - z, 3), p) for t, p in kept]


def selfcheck():
    # synthetic: 80s ramp up, 100s plateau at 400W with one idle dip, 60s down
    rows = []
    t = 0.0
    for i in range(800):
        rows.append((t, 200 + 200 * i / 800)); t += 0.1
    for i in range(1000):
        p = 200.0 if 400 <= i < 500 else 400.0   # deep dip mid-plateau
        rows.append((t, p)); t += 0.1
    for i in range(600):
        rows.append((t, 400 - 200 * i / 600)); t += 0.1
    a, b, plateau, thr = detect_window(rows)
    fail = 0
    # tolerance = the documented over-estimate, not an exact-boundary claim
    if not (77 <= a <= 80.5):
        fail += 1; print(f"FAIL start {a:.1f} not in [77,80.5]", file=sys.stderr)
    if not (180 <= b <= 183):
        fail += 1; print(f"FAIL end {b:.1f} not in [180,183]", file=sys.stderr)
    out = trim(rows, a, b)
    if not (100 <= out[-1][0] <= 106):
        fail += 1; print(f"FAIL dur {out[-1][0]:.1f} not in [100,106]", file=sys.stderr)
    # the mid-plateau dip must survive the trim, not truncate it
    if min(p for _, p in out) > 250:
        fail += 1; print("FAIL dip was lost", file=sys.stderr)
    print("selfcheck: OK" if not fail else f"selfcheck: {fail} FAILED")
    return fail


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--selfcheck":
        sys.exit(selfcheck())
    src = sys.argv[1]
    rows = load(src)
    a, b, plateau, thr = detect_window(rows)
    out = trim(rows, a, b)
    print(f"{src}: plateau={plateau:.1f}W thr={thr:.1f}W  window=[{a:.1f},{b:.1f}]s "
          f"-> {out[-1][0]:.1f}s (was {rows[-1][0]:.1f}s)")
    if len(sys.argv) > 2:
        with open(sys.argv[2], "w", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(["time_s", "power_W"])
            wr.writerows((f"{t:.3f}", f"{p:.3f}") for t, p in out)

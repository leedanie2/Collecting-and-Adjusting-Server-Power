#!/usr/bin/env python3
"""Trim a ramp.c smoother trace to its real-workload window.

ramp.c pre-ramps ballast before launching the real workload and ramps it back
down after, padding the trace by 2.1-2.4x. That padding is slow, near-flat power
with almost no grid-ramp exceedances, so grid_metrics.annualize()
(count * SECONDS_PER_YEAR / trace_duration) divides the SAME real exceedance
events by a longer duration and reports a fake "risk reduction" that tracks the
trace-length ratio, not smoother efficacy. Trim the padding so each ramp.c trace
is scored over the real-workload window only, duration-matched to its baseline.

Offsets are ramp.c's own debug-log tick counts (0.1 s/tick) — real-workload
launch (up) and ramp-down start (down):

    hpl_rampc     up 86.7 s   down 49.8 s   -> real window ~120.4 s (baseline 119.9)
    aisim2_rampc  up 86.7 s   down 48.5 s   -> real window ~120.7 s (baseline 121.0)
    step_rampc    up 62.0 s   down 62.0 s   -> real window ~143.1 s (baseline 110.8)

hpl/aisim2 land within ~0.4 s of their baselines, validating the offsets. step's
window stays ~1.3x longer than baseline because ramp.c's core-affinity cap dilates
the step workload's own internal timing (documented limitation, not padding);
annualizing each trace by its own real-workload duration is still fair once the
ballast padding is gone.

Keeps samples with t0+up <= t <= tend-down, then re-zeros time to start at 0.

    trim_rampc.py <in.csv> <up_s> <down_s> [out.csv]   # out defaults to in (overwrite)
    trim_rampc.py --selfcheck
"""
import csv, sys


def trim(rows, up, down):
    """rows: list of (t, w) floats. Keep [t0+up, tend-down], re-zero time to 0."""
    t0, tend = rows[0][0], rows[-1][0]
    lo, hi = t0 + up, tend - down
    kept = [(t, w) for t, w in rows if lo <= t <= hi]
    if len(kept) < 2:
        raise SystemExit(f"trim left {len(kept)} rows — bad offsets (up={up} down={down})?")
    z = kept[0][0]
    return [(t - z, w) for t, w in kept]


def read_csv(path):
    with open(path) as f:
        r = csv.reader(f)
        next(r)  # header (positional; text ignored)
        return [(float(a), float(b)) for a, b, *_ in r]


def write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_s", "power_W"])
        for t, p in rows:
            w.writerow([f"{t:.3f}", f"{p:.3f}"])


def _selfcheck():
    src = [(float(i), 100.0 + i) for i in range(11)]      # t=0..10, w=100..110
    out = trim(src, up=2, down=3)                          # keep t in [2, 7]
    assert [t for t, _ in out] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0], out
    assert [w for _, w in out] == [102, 103, 104, 105, 106, 107], out
    # too-aggressive offsets must fail loudly, not emit a 1-row trace
    try:
        trim(src, up=6, down=6); assert False, "should have raised"
    except SystemExit:
        pass
    print("SELFCHECK OK")


def main():
    if "--selfcheck" in sys.argv:
        _selfcheck(); return
    if len(sys.argv) < 4:
        raise SystemExit(__doc__)
    inp, up, down = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
    out = sys.argv[4] if len(sys.argv) > 4 else inp
    src = read_csv(inp)
    rows = trim(src, up, down)
    write_csv(out, rows)
    print(f"{inp} -> {out}: {len(src)} -> {len(rows)} rows, "
          f"span {src[-1][0]-src[0][0]:.1f}s -> real window {rows[-1][0]:.1f}s")


if __name__ == "__main__":
    main()

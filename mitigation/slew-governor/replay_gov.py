"""Offline closed-loop replay of the --slew governor against a recorded trace.

Drives the REAL SlewGovernor (rapl_capper.SlewGovernor.step) with simulated
time over a fake powercap tree -- no root, no sleeping, deterministic, ~ms per
run. The recorded trace is treated as workload DEMAND; each tick the plant
model turns demand + the governor's written cap + its ballast request into
measured power:

    workload = min(demand, cap_total - UNDERSHOOT_W)  when demand presses the cap
    measured = workload + ballast_cores * w_per_core  (ballast is real watts)

UNDERSHOOT_W models RAPL settling below the written limit (live: ~25-45 W).
This is a DIRECTIONAL model -- the live run is the arbiter -- but the gated
governor's stock-when-quiet / shaped-edges behavior reproduces here.

Usage:
    .venv/bin/python -m actuators.replay_gov --trace path/to/trace.csv
    .venv/bin/python -m actuators.replay_gov --trace trace.csv --sweep
    .venv/bin/python -m actuators.replay_gov --selfcheck

Per run: max up/down ramps over 0.5 s windows (the live plot's metric),
engage duty, ballast peak, and throttle deficit (mean W shaved off demand --
the perf proxy).
"""

import argparse
import bisect
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rapl_capper as rc  # noqa: E402

UNDERSHOOT_W = 30.0
TICK_S = rc.GOV_POLL_S
W_PER_CORE = 3.26


def load_trace(path):
    """CSV time_s,power_W -> ([t], [W])"""
    ts, ps = [], []
    for ln in Path(path).read_text().splitlines()[1:]:
        if not ln.strip():
            continue
        a, b = ln.split(",")[:2]
        ts.append(float(a))
        ps.append(float(b))
    if len(ts) < 10:
        raise SystemExit(f"trace too short: {path}")
    return ts, ps


def demand_at(ts, ps, t):
    """Linear interpolation, clamped to the trace ends."""
    if t <= ts[0]:
        return ps[0]
    if t >= ts[-1]:
        return ps[-1]
    i = bisect.bisect_right(ts, t)
    t0, t1, p0, p1 = ts[i - 1], ts[i], ps[i - 1], ps[i]
    return p0 + (p1 - p0) * (t - t0) / (t1 - t0)


def build_tree(tmp):
    """Two-zone fake tree matching mycroft: PL1 205 / PL2 246 per socket."""
    zones = []
    for z in (0, 1):
        t = tmp / f"t{z}"
        t.mkdir()
        (t / "name").write_text(f"package-{z}\n")
        (t / "constraint_0_name").write_text("long_term\n")
        (t / "constraint_1_name").write_text("short_term\n")
        (t / "constraint_0_power_limit_uw").write_text("205000000\n")
        (t / "constraint_1_power_limit_uw").write_text("246000000\n")
        (t / "energy_uj").write_text("0\n")
        (t / "max_energy_range_uj").write_text("262143328850\n")
        zones.append(t)
    return zones


def replay(trace_ts, trace_ps, ballast_max=64.0, jump_w=None,
           engage_on_dips=True, disengage_s=None, risk_fn=None,
           peak_hold_s=None, preburn=True):
    """Run the real governor over the trace. Returns (times, measured,
    engaged[], ballast[]) at TICK_S resolution."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        zones = build_tree(tmp)
        risk_file = None
        if risk_fn is not None:
            risk_file = tmp / "risk.flag"
            risk_file.write_text("0\n")
        ns = argparse.Namespace(rapl_root=str(tmp / "t*"), cap_watts=None,
                                floor_watts=None, allow_below_floor=True,
                                tau=1.6, max_cap_s=5.0, cooldown=0.0,
                                events=str(tmp / "ev.jsonl"), dry_run=False)
        capper = rc.setup_capper(ns)
        kw = {} if peak_hold_s is None else dict(peak_hold_s=peak_hold_s)
        gov = rc.SlewGovernor(capper, rc.PowerMeter(zones), len(zones),
                              ballast=None, ballast_w_per_core=W_PER_CORE,
                              risk_file=str(risk_file) if risk_file else None,
                              **kw)
        gov.ballast_cores_n = int(ballast_max)   # match the live pool size
        gov.preburn = preburn and risk_fn is not None   # live: on with --ballast
        ach_cell = [0.0]                         # plant's actual burn, cores
        gov.ballast_achieved_fn = lambda: ach_cell[0]
        if jump_w is not None:
            gov.jump_w = jump_w
        if disengage_s is not None:
            gov.disengage_s = disengage_s
        gov.engage_on_dips = engage_on_dips

        t0, dur = trace_ts[0], trace_ts[-1] - trace_ts[0]
        now, t_end = 1000.0, 1000.0 + dur      # simulated monotonic clock
        capacity = max(trace_ps)   # box's demonstrated max draw: SCHED_IDLE
                                   # ballast only burns capacity real work
                                   # isn't using (starves at the plateau)
        times, meas, engs, balls, sets = [], [], [], [], []
        ballast_set = 0.0
        while now < t_end:
            t_trace = t0 + (now - 1000.0)
            demand = demand_at(trace_ts, trace_ps, t_trace)
            cap = sum(int(rc.read_raw(p)) for p in gov.cap_files) / 1e6
            avail = cap - UNDERSHOOT_W
            workload = min(demand, avail) if demand > avail else demand
            # scheduler starves ballast of what real work claims. No cap term:
            # the fixed-offset undershoot makes cap-vs-ballast ordering spiral
            # (hug tracks power, avail tracks hug), and cap-low + ballast-high
            # can't co-occur by design (risk pre-burns instead of pre-hugging)
            ballast_act = min(ballast_set * W_PER_CORE,
                              max(0.0, capacity - demand))
            ach_cell[0] = ballast_act / W_PER_CORE
            p = workload + ballast_act
            if risk_fn is not None:
                risk_file.write_text("1\n" if risk_fn(t_trace) else "0\n")
            gov.step(now, p)
            ballast_set = min(gov._ballast_set, ballast_max)
            times.append(t_trace)
            meas.append(p)
            engs.append(gov.engaged)
            balls.append(ballast_act / W_PER_CORE)
            sets.append(ballast_set)
            now += TICK_S
        capper.exit_restore()
    return times, meas, engs, balls, sets


def score(times, meas, engs, balls, trace_ts, trace_ps, win=0.5, sets=None):
    dt = times[1] - times[0] if len(times) > 1 else TICK_S
    k = max(1, int(round(win / dt)))
    ramps = [(meas[i + k] - meas[i]) / (times[i + k] - times[i])
             for i in range(len(meas) - k)]
    deficit = [max(0.0, demand_at(trace_ts, trace_ps, t) - (m - b * W_PER_CORE))
               for t, m, b in zip(times, meas, balls)]
    s = dict(max_up=max(ramps), max_down=min(ramps),
             engage_duty=sum(engs) / len(engs),
             ballast_peak=max(balls),
             mean_deficit_w=sum(deficit) / len(deficit),
             mean_w=sum(meas) / len(meas))
    if sets is not None:
        # contention proxy the power plant can't see: core-seconds the pool
        # is RUNNABLE while real work is busy -- starved spinners still steal
        # scheduler/cache time from MPI ranks (gov14 live: 81.6% Gflops)
        busy = 0.8 * max(trace_ps)
        s["runnable_busy_cs"] = sum(
            v * dt for t, v in zip(times, sets)
            if demand_at(trace_ts, trace_ps, t) > busy)
    return s


def score_raw(ts, ps):
    return score(ts, ps, [False] * len(ts), [0.0] * len(ts), ts, ps)


def fmt(s):
    out = (f"up {s['max_up']:+7.0f} W/s  down {s['max_down']:+7.0f} W/s  "
           f"engaged {s['engage_duty']*100:5.1f}%  ballast<= {s['ballast_peak']:4.1f}c  "
           f"deficit {s['mean_deficit_w']:5.1f} W  mean {s['mean_w']:5.1f} W")
    if "runnable_busy_cs" in s:
        out += f"  busyRun {s['runnable_busy_cs']:5.0f} c-s"
    return out


def square_trace(idle=200.0, busy=410.0, period=21.0, dip=3.0, n_cycles=5,
                 dt=0.1, lead_in=8.0):
    """ai_load-shaped synthetic square wave."""
    ts, ps = [], []
    t, end = 0.0, lead_in + n_cycles * period
    while t <= end:
        if t < lead_in:
            p = idle
        else:
            ph = (t - lead_in) % period
            p = busy if ph <= period - dip else idle
        ts.append(t)
        ps.append(p)
        t += dt
    return ts, ps


def selfcheck():
    results = []

    def check(name, ok, detail=""):
        results.append(bool(ok))
        print(("PASS" if ok else "FAIL") + f": {name}" +
              (f"  [{detail}]" if detail and not ok else ""), flush=True)

    ts, ps = square_trace()
    raw = score_raw(ts, ps)

    t1, m1, e1, b1, x1 = replay(ts, ps, peak_hold_s=0.5)
    s = score(t1, m1, e1, b1, ts, ps, sets=x1)
    # a 0.1 s square-wave edge is the worst case for a REACTIVE governor: one
    # tick of latency passes before anything can respond (CP4: onsets are
    # sub-100 ms steps), so the windowed max ramp floors at ~edge/2. Sustained
    # ramps beyond that first tick are slew-shaped.
    check("replay shapes the square wave: both max ramps cut >=40% vs raw "
          "(single-tick latency floor)",
          s["max_up"] < raw["max_up"] * 0.6
          and s["max_down"] > raw["max_down"] * 0.6,
          f"raw={fmt(raw)} got={fmt(s)}")
    check("engage duty stays partial (gated governor, not a permanent hug)",
          0.02 < s["engage_duty"] < 0.9, fmt(s))
    check("ballast fills dips (peak fill > 20 cores)", s["ballast_peak"] > 20.0,
          fmt(s))
    check("throttle deficit small (perf proxy; plateau must run ~uncapped)",
          s["mean_deficit_w"] < 12.0, fmt(s))

    # gov10 back-test: a flat trace must never engage (stock throughout);
    # July-20 idle-FP guard: a flapping risk flag must not pre-burn it either.
    flat_ts = [i * 0.1 for i in range(400)]
    flat_ps = [200.0] * 400
    _, _, e2, _, _ = replay(flat_ts, flat_ps)
    _, _, e2r, b2r, x2r = replay(flat_ts, flat_ps,
                                  risk_fn=lambda t: t >= 35.0)
    check("flat trace: governor never engages, and a high risk flag cannot "
          "pre-burn idle power (stock caps throughout)",
          not any(e2) and not any(e2r) and max(b2r) == 0.0 and max(x2r) == 0.0)

    # risk pre-engage: flag high across the first onset -> it lands on an
    # already-hugging ceiling. Judge the ONSET window itself (the global max
    # sits at a later resume the flag never covered).
    def window_max_up(times, meas, lo, hi, win=0.5):
        k = max(1, int(round(win / TICK_S)))
        return max((meas[i + k] - meas[i]) / (times[i + k] - times[i])
                   for i in range(len(meas) - k) if lo <= times[i] <= hi)

    t3, m3, e3, b3, x3 = replay(ts, ps, peak_hold_s=0.5,
                            risk_fn=lambda t: 4.0 <= t <= 9.0)
    onset_risk = window_max_up(t3, m3, 7.0, 10.0)
    onset_reactive = window_max_up(t1, m1, 7.0, 10.0)
    check("detector lead shapes the step onset itself (<200 W/s in the onset "
          "window; reactive alone cannot)",
          onset_risk < 200.0 < onset_reactive,
          f"risk_onset={onset_risk:.0f} reactive_onset={onset_reactive:.0f}")

    # pre-burn: same led onset, but shaping must come from BURN (ballast ramps
    # before the step, work swaps in watt-for-watt) not throttle -- near-zero
    # onset-window deficit, vs the pre-hug path which necessarily throttles
    def window_deficit(times, meas, balls, lo, hi):
        return max(demand_at(ts, ps, t) - (m - b * W_PER_CORE)
                   for t, m, b in zip(times, meas, balls) if lo <= t <= hi)
    check("pre-burn ramps ballast on the risk lead (>40 cores before onset)",
          max(b for t, b in zip(t3, b3) if t < 8.0) > 40.0,
          f"pre-onset ballast peak={max(b for t, b in zip(t3, b3) if t < 8.0):.1f}c")
    check("pre-burn onset is burn-shaped, not throttle-shaped "
          "(onset-window deficit < 10 W)",
          window_deficit(t3, m3, b3, 7.9, 11.0) < 10.0,
          f"deficit={window_deficit(t3, m3, b3, 7.9, 11.0):.1f}W")
    check("pre-burn parks the pool the moment work swaps in (runnable-while-"
          "busy < 20 core-seconds; runnable starved spinners steal MPI time)",
          score(t3, m3, e3, b3, ts, ps, sets=x3)["runnable_busy_cs"] < 20.0,
          f"busy_cs={score(t3, m3, e3, b3, ts, ps, sets=x3)['runnable_busy_cs']:.1f}")
    t4, m4, e4, b4, x4 = replay(ts, ps, peak_hold_s=0.5, preburn=False,
                            risk_fn=lambda t: 4.0 <= t <= 9.0)
    check("pre-hug control still throttles the led onset (deficit > 20 W; "
          "proves pre-burn is doing the work)",
          window_deficit(t4, m4, b4, 7.9, 11.0) > 20.0,
          f"deficit={window_deficit(t4, m4, b4, 7.9, 11.0):.1f}W")

    ok = all(results)
    print(("SELFCHECK OK: " if ok else "SELFCHECK FAILED: ")
          + f"{sum(results)}/{len(results)} assertions passed", flush=True)
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trace", help="recorded time_s,power_W CSV (demand)")
    ap.add_argument("--sweep", action="store_true",
                    help="sweep JUMP_ENGAGE_W x engage-on-dips x disengage window")
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()

    if args.selfcheck:
        sys.exit(selfcheck())
    if not args.trace:
        ap.error("--trace required (or --selfcheck)")

    ts, ps = load_trace(args.trace)
    print(f"raw trace                        {fmt(score_raw(ts, ps))}")
    if not args.sweep:
        t, m, e, b, x = replay(ts, ps)
        print(f"governor (shipped params)        {fmt(score(t, m, e, b, ts, ps, sets=x))}")
        return
    for jump in (40.0, 45.0, 60.0):
        for dips in (True, False):
            for dis in (1.0, 2.0):
                for ph in (1.5, 0.5):
                    t, m, e, b, x = replay(ts, ps, jump_w=jump, engage_on_dips=dips,
                                           disengage_s=dis, peak_hold_s=ph)
                    s = score(t, m, e, b, ts, ps, sets=x)
                    print(f"jump={jump:3.0f} dips={int(dips)} dis={dis:3.1f} "
                          f"ph={ph:3.1f}  {fmt(s)}")


if __name__ == "__main__":
    main()

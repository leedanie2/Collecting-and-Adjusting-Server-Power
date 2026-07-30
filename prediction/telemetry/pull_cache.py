#!/usr/bin/env python3
"""Pull cpu_power/cpu_freq/cpu_temp/cpu_usage from InfluxDB into a local cache.

Incremental: resumes from the cache's last timestamp and appends, pulling in
day-sized chunks so no single Flux query can hit the timeout (a 13-day gap
after time off the lab network killed the old full-history pull, 2026-07-08).
--start forces a full re-pull from the given time, overwriting the cache.
"""

import argparse

import pandas as pd

from core import telemetry as common

CHUNK = pd.Timedelta(days=1)


def main():
    p = argparse.ArgumentParser(description="Pull telemetry from InfluxDB into a local cache.")
    p.add_argument("--start", default=None,
                   help="Flux range start; forces a full re-pull overwriting the cache "
                        "(default: incremental since the cache's last timestamp)")
    args = p.parse_args()

    old = None
    if args.start is not None:
        start_ts = pd.Timestamp(args.start)
    elif common.CACHE_PATH.exists():
        old = common.load_telemetry()
        start_ts = old.index.max()
    else:
        start_ts = pd.Timestamp("1970-01-01T00:00:00Z")
    if start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize("UTC")

    now = pd.Timestamp.now(tz="UTC")
    frames = []
    t = start_ts
    while t < now:
        t_stop = min(t + CHUNK, now)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        chunk = common.query_window(t.strftime(fmt), t_stop.strftime(fmt),
                                    measurements=tuple(common.RENAME),
                                    timeout_ms=600_000)
        if not chunk.empty:
            frames.append(chunk)
            print(f"  {t:%Y-%m-%d %H:%M} -> {t_stop:%Y-%m-%d %H:%M}: {len(chunk)} rows")
        t = t_stop

    if old is None and not frames:
        raise SystemExit("No data returned from InfluxDB.")

    wide = pd.concat(([old] if old is not None else []) + frames)
    wide = wide[~wide.index.duplicated(keep="last")].sort_index()
    assert wide.index.is_monotonic_increasing and wide.index.is_unique

    common.DATA_DIR.mkdir(exist_ok=True)
    wide.to_csv(common.CACHE_PATH)
    print(f"Saved {len(wide)} rows ({wide.index.min()} -> {wide.index.max()}) to {common.CACHE_PATH}")


if __name__ == "__main__":
    main()

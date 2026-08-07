#!/usr/bin/env python3
"""fetch_mycroft_trace.py — pull a cpu_power window out of InfluxDB as a
pipeline-ready trace CSV (time_s,power_W).

This is the bridge from mycroft's live telemetry (rapl_hf_sampler ->
InfluxDB "Power" bucket, measurement cpu_power, field watts) to the grid
pipeline's input contract. Any window you can see on the Grafana dashboard
can be replayed through the simulation.

Run ON mycroft (InfluxDB is localhost there). From this workstation, pipe it
over ssh — no file copying needed:

    ssh mycroft python3 - --last 30m  < scripts/fetch_mycroft_trace.py  > data/burst1.csv
    ssh mycroft python3 - --start 2026-07-15T18:35:48Z --stop 2026-07-15T18:37:38Z \
        < scripts/fetch_mycroft_trace.py > data/burst2.csv
    scripts/sim_trace.sh burst1.csv          # straight into the pipeline

Stdlib-only (urllib against the Influx v2 HTTP API) so it runs under any
python3 on the box, no venv. Token/org come from ~/.secrets/ and the org name
is never printed. --selfcheck runs offline against a canned response.
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

INFLUX_URL = "http://localhost:8086"
BUCKET = "Power"
MEASUREMENT = "cpu_power"
FIELD = "watts"


def read_secret(*names):
    for n in names:
        p = Path.home() / ".secrets" / n
        if p.exists():
            return p.read_text().strip()
    sys.exit(f"none of ~/.secrets/{{{','.join(names)}}} found")


def parse_rfc3339(s):
    """Influx returns nanosecond RFC3339 ('...T18:35:48.123456789Z');
    fromisoformat wants <=6 fractional digits and no bare Z."""
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        frac = rest[: rest.index("+")] if "+" in rest else rest
        tz = rest[len(frac):]
        s = f"{head}.{frac[:6]:<06s}{tz}" if frac else head + tz
    return datetime.fromisoformat(s).timestamp()


def annotated_csv_to_rows(text):
    """Extract (epoch_s, value) from Influx annotated CSV. Column positions
    come from each table's header row (they shift with annotations)."""
    rows = []
    it_col = iv_col = None
    for line in text.splitlines():
        if not line or line.startswith("#"):
            it_col = iv_col = None      # annotation block precedes a new header
            continue
        cells = line.split(",")
        if it_col is None:
            if "_time" in cells and "_value" in cells:
                it_col, iv_col = cells.index("_time"), cells.index("_value")
            continue
        if len(cells) > max(it_col, iv_col) and cells[it_col]:
            raw = cells[iv_col]
            if raw == "true":
                raw = "1"
            elif raw == "false":
                raw = "0"
            try:
                rows.append((parse_rfc3339(cells[it_col]), float(raw)))
            except ValueError:
                continue
    rows.sort()
    # dedupe repeated timestamps (keep first) — the pipeline drops them anyway
    out, last_t = [], None
    for t, v in rows:
        if t != last_t:
            out.append((t, v))
            last_t = t
    return out


def rows_to_trace(rows, value_label="power_W", measurement="cpu_power", epoch=False):
    """(epoch, value) -> 'time_s,<value_label>' lines. Rebased to 0 by default;
    with epoch=True the absolute epoch seconds are kept, so a power trace and a
    risk series pulled over the same window land on one shared clock (for scoring)."""
    if not rows:
        sys.exit(f"no {measurement} samples in that window — wrong range, or the writer was down")
    t0 = 0.0 if epoch else rows[0][0]
    lines = [f"time_s,{value_label}"]
    lines += [f"{t - t0:.3f},{v:.3f}" for t, v in rows]
    return lines


def query(flux, token, org):
    req = urllib.request.Request(
        f"{INFLUX_URL}/api/v2/query?org={urllib.parse.quote(org)}",
        data=json.dumps({"query": flux, "type": "flux"}).encode(),
        headers={
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
            "Accept": "application/csv",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode()
    except urllib.error.HTTPError as e:
        # never echo the URL back — it contains the org name
        sys.exit(f"influx query failed: HTTP {e.code} {e.read().decode()[:200]}")


def build_flux(start, stop, measurement=MEASUREMENT, field=FIELD):
    rng = f"start: {start}" + (f", stop: {stop}" if stop else "")
    return (
        f'from(bucket: "{BUCKET}") |> range({rng}) '
        f'|> filter(fn: (r) => r._measurement == "{measurement}" '
        f'and r._field == "{field}") '
        '|> keep(columns: ["_time", "_value"]) |> sort(columns: ["_time"])'
    )


CANNED = """\
#group,false,false,true,true,false,false
#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,dateTime:RFC3339,double
#default,_result,,,,,
,result,table,_start,_stop,_time,_value
,_result,0,2026-07-15T18:00:00Z,2026-07-15T19:00:00Z,2026-07-15T18:35:48.1Z,231.201
,_result,0,2026-07-15T18:00:00Z,2026-07-15T19:00:00Z,2026-07-15T18:35:48.2Z,232.5
,_result,0,2026-07-15T18:00:00Z,2026-07-15T19:00:00Z,2026-07-15T18:35:48.2Z,999.0
,_result,0,2026-07-15T18:00:00Z,2026-07-15T19:00:00Z,2026-07-15T18:35:48.351234567Z,230.0
"""

CANNED_BOOL = """\
#group,false,false,true,true,false,false
#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,dateTime:RFC3339,boolean
#default,_result,,,,,
,result,table,_start,_stop,_time,_value
,_result,0,2026-07-15T18:00:00Z,2026-07-15T19:00:00Z,2026-07-15T18:35:48.1Z,false
,_result,0,2026-07-15T18:00:00Z,2026-07-15T19:00:00Z,2026-07-15T18:35:48.2Z,true
"""


def selfcheck():
    assert parse_rfc3339("2026-07-15T18:35:48Z") == parse_rfc3339("2026-07-15T18:35:48.000Z")
    assert abs(parse_rfc3339("2026-07-15T18:35:48.5Z")
               - parse_rfc3339("2026-07-15T18:35:48Z") - 0.5) < 1e-9

    rows = annotated_csv_to_rows(CANNED)
    assert len(rows) == 3, rows                       # duplicate ts dropped
    assert rows[0][1] == 231.201 and rows[1][1] == 232.5
    assert abs(rows[2][0] - rows[0][0] - 0.251234) < 1e-5   # ns frac truncated to us

    trace = rows_to_trace(rows)
    assert trace[0] == "time_s,power_W"
    assert trace[1] == "0.000,231.201"
    assert trace[2].startswith("0.100,232.5")

    flux = build_flux("-30m", None)
    assert 'range(start: -30m)' in flux and "stop" not in flux
    assert "stop: 2026-01-01T00:00:00Z" in build_flux("-1h", "2026-01-01T00:00:00Z")

    bool_rows = annotated_csv_to_rows(CANNED_BOOL)
    assert len(bool_rows) == 2, bool_rows              # boolean "false"/"true" values kept, not dropped
    assert bool_rows[0][1] == 0.0 and bool_rows[1][1] == 1.0

    rflux = build_flux("-30m", None, "power_usage_edge_prediction", "usage_risk")
    assert 'power_usage_edge_prediction' in rflux and 'usage_risk' in rflux
    et = rows_to_trace(rows, "usage_risk", "power_usage_edge_prediction", epoch=True)
    assert et[0] == "time_s,usage_risk", et[0]
    assert et[1].startswith(f"{rows[0][0]:.3f},"), et[1]   # absolute epoch, not rebased
    print("SELFCHECK OK")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--last", metavar="DUR",
                   help="relative window ending now, e.g. 30m, 2h")
    g.add_argument("--start", metavar="RFC3339", help="window start")
    ap.add_argument("--stop", metavar="RFC3339", help="window stop (with --start)")
    ap.add_argument("--measurement", default=MEASUREMENT,
                    help=f"measurement (default {MEASUREMENT}; risk series: "
                         "power_usage_edge_prediction / power_spike_prediction)")
    ap.add_argument("--field", default=FIELD,
                    help=f"field (default {FIELD}; risk series: usage_risk / spike_risk)")
    ap.add_argument("--epoch", action="store_true",
                    help="keep absolute epoch seconds (no rebase) so a power trace and a "
                         "risk series over the same window share one clock — use for scoring")
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()

    if args.selfcheck:
        selfcheck()
        return
    if not args.last and not args.start:
        ap.error("need --last DUR or --start/--stop")

    token = read_secret("influx_read_token.txt", "influx_token.txt")
    org = read_secret("influx_org.txt")
    start = f"-{args.last.lstrip('-')}" if args.last else args.start
    text = query(build_flux(start, args.stop, args.measurement, args.field), token, org)
    rows = annotated_csv_to_rows(text)
    label = "power_W" if args.field == "watts" else args.field
    print("\n".join(rows_to_trace(rows, label, args.measurement, args.epoch)))
    print(f"captured {len(rows)} samples spanning {rows[-1][0] - rows[0][0]:.1f}s",
          file=sys.stderr)


if __name__ == "__main__":
    main()

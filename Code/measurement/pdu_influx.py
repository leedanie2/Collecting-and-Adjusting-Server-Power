#!/usr/bin/env python3
"""Poll the APC PDU's real power and stream it to InfluxDB.

Builds on pdu_poll.py's session/auth logic. No fixed poll interval: HTTP
round-trip time against the live PDU (~550ms-1.4s, measured) is the rate
limiter, so the loop just re-requests immediately after each write.
"""

import sys
import time
import argparse
from pathlib import Path

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

import pdu_poll

TOKEN_PATH = Path.home() / ".secrets" / "influx_token.txt"
ORG_PATH = Path.home() / ".secrets" / "influx_org.txt"

INFLUX_URL = "http://mycroft:8086"
INFLUX_BUCKET = "Power"

PDU_OUTLET_GROUP = 0


def load_token(path):
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        print(f"Error: token file not found at {path}.", file=sys.stderr)
        sys.exit(1)


def power_point(server, kw):
    """Build the InfluxDB Point for one PDU power reading (kW -> W)."""
    return (
        Point("pdu_power")
        .tag("pdu", str(PDU_OUTLET_GROUP))
        .tag("server", server)
        .field("watts", kw * 1000.0)
    )


def main():
    p = argparse.ArgumentParser(description="Stream APC PDU power telemetry to InfluxDB.")
    p.add_argument("--server", default="mycroft",
                   help="Tag identifying the rack/server this PDU powers (default: %(default)s).")
    args = p.parse_args()

    token = load_token(TOKEN_PATH)
    org = load_token(ORG_PATH)

    client = InfluxDBClient(url=INFLUX_URL, token=token, org=org)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    print("Authenticating with PDU...")
    pdu_poll.PDU_POLL_INIT()

    print(f"[pdu_influx] server={args.server} pdu={PDU_OUTLET_GROUP} "
          f"-> {INFLUX_URL} org={org} bucket={INFLUX_BUCKET}. Ctrl+C to stop.")

    try:
        while True:
            try:
                kw = pdu_poll.PDU_POLL_POWER(PDU_OUTLET_GROUP)
                write_api.write(bucket=INFLUX_BUCKET, org=org,
                                 record=power_point(args.server, kw))
                print(f"[{time.ctime()}] Sent {kw:.2f} kW ({kw * 1000:.0f} W).")
            except RuntimeError as e:
                print(f"[{time.ctime()}] PDU session error ({e}); re-authenticating...",
                      file=sys.stderr)
                pdu_poll.PDU_POLL_FINALIZE()
                pdu_poll.PDU_POLL_INIT()
            except Exception as e:
                print(f"[{time.ctime()}] Error: {e}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\n[pdu_influx] stopped.")
    finally:
        pdu_poll.PDU_POLL_FINALIZE()
        write_api.close()
        client.close()


if __name__ == "__main__":
    main()

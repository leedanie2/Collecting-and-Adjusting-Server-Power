# Measurement (collect)

The telemetry that captures server power for everything else.

```
influx2.py               10 Hz package RAPL + 1 Hz process/system stats into
                         InfluxDB; decoupled samplers, one writer per field
rapl_hf_sampler.c        high-frequency RAPL sampler in C, for lower jitter
                         and overhead than the Python path
collect_rapl.py          capture a RAPL trace alongside a running workload
fetch_mycroft_trace.py   pull any InfluxDB power window out as a
                         time_s,power_W trace CSV — the input format the grid
                         simulation expects
sys_influx.py            Linux system telemetry: run-queue depth, load
                         averages, context-switch rate, memory, disk and
                         network throughput. Pure stdlib, no root — every
                         source file under /proc here is world-readable
redfish_influx.py        out-of-band BMC telemetry over Redfish (power
                         supplies, temperatures, fans) at 0.1 Hz
pdu_influx.py            rack PDU real power over HTTP; no fixed interval,
                         the ~550 ms–1.4 s round trip is the rate limiter
pdu_poll.py              the PDU session/scrape library pdu_influx.py drives
*.service                systemd units for the samplers
```

`sys_influx.py` is the upstream half of the story: the run-queue depth it
records (`/proc/stat`'s `procs_running`) is the signal the detector work leans
on, and it is the one field here that leads a power transient rather than
trailing it.

## Sampling rate

Sampling faster than 10 Hz on this hardware produces phantom kilowatt spikes.
The RAPL energy counter does not advance on every read, so a run of unchanged
reads is followed by one read carrying all the accumulated energy; dividing
that by a single sample interval reports power that never happened. The
samplers only emit a value when the counter actually advanced, dividing the
energy delta by the time since the last advance. See the note in
`collect_rapl.py` for the same artifact seen in the committed traces.

## Credentials and site configuration

RAPL energy counters are root-readable only; the `/proc` sources used by
`sys_influx.py` are not, and the BMC and PDU are reached over the network
instead of the host.

Nothing here embeds a credential or a site address. Secrets load from
`~/.secrets` at 0600:

```
~/.secrets/influx_token.txt        InfluxDB write token
~/.secrets/influx_org.txt          InfluxDB org
~/.secrets/influx_read_token.txt   read token, for the analysis side
~/.secrets/bmc_password.txt        read-only BMC account password
~/.secrets/pdu_password.txt        read-only PDU web password
```

Addresses come from the environment or the unit file — `BMC_IP`, `BMC_USER`,
`PDU_URL`, `PDU_USER` — with `BMC_PASSWORD_FILE` and `PDU_PASSWORD_FILE` to
relocate the password files (a root-run unit has a different `HOME`).

Each daemon takes `--selfcheck` to authenticate, collect one round, assert the
expected fields are present, and exit without writing.

This all ran on the instrumented server; reproducing it needs that hardware,
a BMC, and a networked PDU.

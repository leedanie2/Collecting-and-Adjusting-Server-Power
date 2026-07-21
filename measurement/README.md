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
*.service                systemd units for the samplers
```

RAPL energy counters are root-readable only. Credentials load from `~/.secrets`
and are never written into the code. This ran on the instrumented server.

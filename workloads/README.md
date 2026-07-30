# Workloads

The benchmarks driven on the server to produce realistic power envelopes. Three
were used, chosen to have different power shapes: a cyclic compute/communicate
duty cycle, an abrupt inference-style cliff, and a programmable staircase.

```
ai_load.sh + HPL.dat   128-rank HPL (High-Performance Linpack) with an
                       18 s-busy / 3 s-dip cycle, approximating an AI-training
                       power envelope rather than a flat plateau
ai_sim_2.py            AI-style workload, 128 workers (contributed by a
                       collaborator)
load.c                 stepped load generator driven by a waveform file
                       (contributed by a collaborator)
step.waveform          the waveform used for the benchmark runs
hpl_setup_summary.md   HPL build and configuration notes
```

## HPL

`ai_load.sh` drives a 128-rank HPL solve (N = 80,000, NB = 192, P×Q = 8×16,
a 51.2 GB working set) and imposes the duty cycle itself by sending
SIGSTOP/SIGCONT to each rank with a 3 ms stagger. Because the job is
completion-based rather than fixed-duration, its runtime varies with anything
that slows it down — which is exactly what makes it the workload the cost
numbers come from. Sustained Gflops is read from the `WR00R2R4` line.

## AI simulation

`ai_sim_2.py` spawns one worker per logical core and alternates rest and
prefill phases over a 120 s window, with three bursts of at least 5 s. Workers
poll a shared state flag every 2–5 ms during prefill, so a phase ending drops
all cores essentially at once and produces a near-vertical power cliff. The
schedule is drawn from a seed that the run prints, so `--seed` reproduces an
identical schedule across conditions.

Note that the seed only fixes the schedule, not the power. This is the one
workload whose traces show the RAPL sampling artifact discussed in
`../measurement/`.

## Step

`./load <threads> <waveform>` runs the given number of worker threads through a
list of instructions, each holding a target CPU utilization for a duration by
alternating arithmetic and sleep inside a 4 ms window. Because utilization and
package power track each other closely, a utilization staircase is an effective
way to author a power profile.

`step.waveform` is the one used for benchmarking: 110 s over nine instructions,
exercising partial levels as well as full, both directions, and steps of
several sizes, rather than only the idle-to-peak cliff the other two produce.
The file format is `<time_ms>, <percent>` per line, with `#` comments and blank
lines ignored, so other profiles are easy to write.

```bash
cc -O2 -pthread -o load load.c -lm
./load 128 step.waveform
```

Traces captured from these runs are under `../grid-simulation/data/traces/`.

# Workloads

The benchmarks driven on the server to produce realistic power envelopes.

```
ai_load.sh + HPL.dat   128-rank HPL (High-Performance Linpack) with an
                       18 s-busy / 3 s-dip cycle, approximating an AI-training
                       power envelope rather than a flat plateau
ai_sim_2.py            AI-style workload, 128 workers (contributed by a
                       collaborator)
load.c                 stepped load generator driven by a waveform file
                       (contributed by a collaborator)
hpl_setup_summary.md   HPL build and configuration notes
```

Traces captured from these runs are under `../grid-simulation/data/traces/`.

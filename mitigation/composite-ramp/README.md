# composite_ramp.c — not yet in this repository

`composite_ramp.c` builds on `../ramp/ramp.c` and is described in the paper, but
its source is not included here yet. It was written by a collaborator and will
be added in a later commit.

This file exists so the gap is explicit rather than silent: a reader who finds
the design in the paper and no code should know it is pending, not missing by
oversight.

## What it does

Where `ramp.c` shapes one workload's power with a fixed linear ramp,
`composite_ramp.c` puts a single manager process in charge of an array of
worker processes, which handle power in two distinct situations.

**Filling** runs alongside a primary task. Workers are pinned to the same
logical cores as that task and run floating-point work at `SCHED_IDLE`, so
whenever the primary task leaves its allocated cores idle the Linux scheduler
hands them to a worker, and whenever the primary task wants them back the
workers yield immediately. The effect is to hold power flat across the primary
task's own internal dips without slowing it down.

**Transitioning** runs between primary tasks. The manager classifies cores by
whether each was allocated to the outgoing task, the incoming task, both, or
neither. Cores in both sets are handled by worker replacement — the worker on
an outgoing core is destroyed as a fully active worker is created on an
incoming one. Cores left over on either side are then ramped: leftovers from
the outgoing task ramp down to zero and are destroyed, leftovers for the
incoming task get new workers ramped up from idle, one worker at a time.

## Where it sits in the results

The measured cost of `ramp.c` — which is the ramping approach without any of
the above — is large: on the HPL workload it roughly doubles runtime and
increases energy by about 160% when the ramps are counted. Filling is what
makes that cost scale with how much of its allocation the primary task actually
uses, which is why the approach is expensive for workloads that leave most of
their cores idle.

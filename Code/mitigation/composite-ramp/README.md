# composite-ramp

`composite_ramp.c` generalises `../ramp/ramp.c` from one workload to a sequence
of them. Where `ramp.c` ramps a fixed set of cores up, runs a single command,
and ramps down, `composite_ramp.c` reads a list of instructions and shapes the
transitions *between* consecutive workloads as well as the ends.

Contributed by a collaborator and checked in as written.

```bash
cc -O2 -Wall -o composite_ramp composite_ramp.c -lpthread -lm
./composite_ramp example.instructions.csv
```

## The instruction file

One instruction per line, three comma-separated fields, no comment syntax —
blank lines are skipped but a `#` line is a parse error:

```
<ramp_rate>,<start:end>,<command>
```

`ramp_rate` is percent of a core per second (200 = 0.5 s to bring one core from
idle to full). `start:end` is an inclusive, contiguous core range. `command` is
run with `execvp`, pinned to exactly that range, and is waited on before the
next instruction begins.

The final line must be the sentinel `<ramp_rate>,<ignored>,exit`. Its rate
drives the closing ramp-down; without it the program has no rate to finish with
and exits with an error rather than guessing.

## What it does

**Filling** runs alongside each command. One worker thread is pinned per core
at `SCHED_IDLE`, cycling busy/idle against a 10 ms period to hold an atomic
duty-cycle target. Because the primary workload is pinned to the same cores at
normal priority, the scheduler hands a core to its worker only when the primary
leaves it idle, and preempts the worker the moment the primary wants it back.
Power stays flat across the workload's internal dips at close to zero cost to
the workload itself.

**Transitioning** runs between commands. The manager compares the outgoing core
range to the incoming one and splits the difference into cores leaving, cores
arriving, and cores common to both. Departures and arrivals are then paired off
one-for-one: an outgoing worker is stopped as a replacement is created already
at 100%, so the two cancel and aggregate draw does not move. Only the unpaired
remainder is ramped — leftover outgoing cores ramp down to zero and are
destroyed (highest core first), leftover incoming cores ramp up from idle (one
at a time, ascending). Ramps close the loop on `/proc/stat`: a core is not
considered finished until its measured utilisation agrees with its target.

## How this differs from the paper's description

The paper describes the manager and workers as processes and classifies cores
by set membership. The implementation uses one process with pthreads, and
tracks the active set as a contiguous `start:end` range rather than an
arbitrary set — so a transition between two non-contiguous core sets is not
expressible. The pairing and ramping logic is otherwise as described.

## Why it is not in the results

The measured matrix in `../../grid-simulation/` runs every `rampc` cell through
`ramp.c`, not this. Each cell is a single workload, and on a single workload
the two programs do the same thing — there is no transition between commands
for the composite logic to shape, so it buys nothing measurable. Routing one
cell through it and not the others would also have made that cell incomparable
to the rest of the matrix. See the comment in
`../../grid-simulation/scripts/recollect.sh`.

Its value is for the case the matrix does not cover: back-to-back jobs on a
shared machine, where the gap between one job ending and the next beginning is
exactly the transient this repository is about.

## Caveats

Unlike most modules here, this one has no `--selfcheck`; it is verified by
running it. It also needs cores that exist — the range is validated against
`MAX_CORES` (128), not against the host's actual CPU count, so an out-of-range
core yields affinity warnings and a `/proc/stat` lookup failure rather than a
clean error.

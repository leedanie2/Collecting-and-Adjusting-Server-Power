/*
 * cpu_dummy_worker.c  (v7 — hot-standby optimised)
 *
 * Continuous FMA workload with dynamically-adjustable intensity.
 *
 * v7 changes vs v6
 * ----------------
 *   SLEEP_NS_IDLE   5 000 000 ns (5 ms)  →  100 000 ns (100 µs)
 *
 *   When intensity < 0.01 the worker now re-reads the control file on
 *   EVERY idle sleep cycle instead of waiting for CTRL_INTERVAL ticks.
 *   This means the activation latency after the orchestrator writes 1.0
 *   is at most one idle sleep period — approximately 100 µs.
 *
 * Control protocol
 * ----------------
 *   Write a float in [0.0, 1.0] to CTRL_FILE.  The worker re-reads it
 *   every CTRL_INTERVAL outer ticks at full load, or every 100 µs when
 *   idle.  intensity=1.0 → full load; intensity=0.0 → idle poll.
 *
 * Build
 * -----
 *   gcc -O2 -march=native -o cpu_dummy_worker cpu_dummy_worker.c -lm
 *
 * Run
 * ---
 *   ./cpu_dummy_worker [base_iters_per_tick]
 *   (default: 2,000,000 — tuned for ~50 ms per tick on a modern core)
 */

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <signal.h>
#include <string.h>
#include <math.h>
#include <time.h>

#define CTRL_FILE           "/tmp/dummy_intensity"
#define CTRL_INTERVAL       4            /* re-read file every N ticks at full load */
#define DEFAULT_BASE_ITERS  2000000ULL   /* FMA ops per tick at intensity=1.0       */
#define SLEEP_NS_IDLE       100000L      /* 100 µs — tight idle poll for hot standby */

static volatile sig_atomic_t g_stop = 0;

static void on_signal(int s) { (void)s; g_stop = 1; }

/* Read intensity from CTRL_FILE; returns 1.0 on any read failure. */
static double read_intensity(void)
{
    FILE *f = fopen(CTRL_FILE, "r");
    if (!f) return 1.0;
    double v = 1.0;
    fscanf(f, "%lf", &v);
    fclose(f);
    if (v < 0.0) v = 0.0;
    if (v > 1.0) v = 1.0;
    return v;
}

/*
 * Inner FMA kernel.
 * Uses inline asm so the compiler cannot eliminate or hoist the loop.
 * Falls back to a scalar expression on non-FMA hardware.
 */
static volatile double g_sink = 0.0;

static void run_fma_batch(uint64_t iters)
{
    double v = g_sink;
    double a = 1.0000000001, b = 0.9999999999;

#if defined(__FMA__)
    for (uint64_t i = 0; i < iters; i++)
        __asm__ volatile("vfmadd231sd %2, %1, %0"
                         : "+x"(v) : "x"(a), "x"(b));
#else
    for (uint64_t i = 0; i < iters; i++)
        v = v * a + b;
#endif

    g_sink = v;
}

int main(int argc, char **argv)
{
    uint64_t base = (argc > 1)
        ? (uint64_t)strtoull(argv[1], NULL, 10)
        : DEFAULT_BASE_ITERS;

    signal(SIGTERM, on_signal);
    signal(SIGINT,  on_signal);

    double   intensity  = read_intensity();
    uint64_t ctrl_count = 0;

    while (!g_stop) {
        /*
         * Idle path: re-read the control file every 100 µs so the worker
         * can activate within one sleep cycle of the orchestrator writing 1.0.
         */
        if (intensity < 0.01) {
            struct timespec ts = { 0, SLEEP_NS_IDLE };
            nanosleep(&ts, NULL);
            intensity  = read_intensity();
            ctrl_count = 0;
            continue;
        }

        /* Full-load path: refresh intensity every CTRL_INTERVAL ticks. */
        if (++ctrl_count >= (uint64_t)CTRL_INTERVAL) {
            intensity  = read_intensity();
            ctrl_count = 0;
        }

        uint64_t iters = (uint64_t)((double)base * intensity);
        run_fma_batch(iters);
    }

    return 0;
}

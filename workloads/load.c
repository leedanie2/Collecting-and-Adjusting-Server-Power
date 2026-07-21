/*
 * load.c - Generate controlled CPU load across N worker threads.
 *
 * Usage: ./load <N> <instructions_file.csv>
 *
 * The instructions file contains one instruction per line:
 *     <time (ms)>, <percent usage (%)>
 *
 * All N worker threads execute the same instruction sequence in lockstep
 * order (first line first). For each instruction they spend `time` ms
 * loading the CPU to roughly `percent` utilization by alternating between
 * busy arithmetic/memory work and sleeping (a duty cycle).
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <time.h>
#include <pthread.h>

/* Length of one duty-cycle window in nanoseconds (4 ms).
 * Within each window the thread burns CPU for `percent`% of the window
 * and sleeps for the remainder, yielding the requested average usage. */
#define WINDOW_NS (4L * 1000L * 1000L)

typedef struct {
    long   time_ms;   /* how long to sustain this load, milliseconds */
    double percent;   /* target CPU usage, 0..100                    */
} instruction_t;

typedef struct {
    const instruction_t *instrs;
    size_t               count;
    int                  id;
} worker_args_t;

/* Monotonic clock helper: current time in nanoseconds. */
static long long now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

/* Sleep for the given number of nanoseconds, restarting on signal. */
static void sleep_ns(long ns)
{
    if (ns <= 0)
        return;
    struct timespec req;
    req.tv_sec  = ns / 1000000000L;
    req.tv_nsec = ns % 1000000000L;
    while (nanosleep(&req, &req) == -1 && errno == EINTR)
        ; /* retry with remaining time written back into req */
}

/*
 * Busy-loop performing arithmetic and memory operations until `deadline_ns`
 * (a CLOCK_MONOTONIC timestamp in ns) is reached. The `sink` pointer keeps
 * the compiler from optimizing the work away and touches memory each pass.
 */
static void burn_until(long long deadline_ns, volatile double *sink)
{
    double acc = *sink;
    while (now_ns() < deadline_ns) {
        /* A chunk of arithmetic + memory traffic between clock checks so
         * we are not dominated by the cost of now_ns() itself. */
        for (int i = 0; i < 4096; i++) {
            acc = acc * 1.0000001 + 1.0;
            acc -= (double)(i & 0x3f);
            *sink = acc;          /* memory write */
            acc += *sink * 0.5;   /* memory read  */
        }
    }
    *sink = acc;
}

/* Run a single instruction: hold `percent` usage for `time_ms` ms. */
static void run_instruction(const instruction_t *ins, volatile double *sink)
{
    double pct = ins->percent;
    if (pct < 0.0)   pct = 0.0;
    if (pct > 100.0) pct = 100.0;

    long long start    = now_ns();
    long long end      = start + (long long)ins->time_ms * 1000000LL;
    long      busy_ns  = (long)(WINDOW_NS * (pct / 100.0));
    long      idle_ns  = WINDOW_NS - busy_ns;

    while (now_ns() < end) {
        long long window_start = now_ns();

        if (busy_ns > 0) {
            long long busy_deadline = window_start + busy_ns;
            if (busy_deadline > end)
                busy_deadline = end;
            burn_until(busy_deadline, sink);
        }

        if (idle_ns > 0) {
            /* Don't oversleep past the instruction's end. */
            long long remaining = end - now_ns();
            long      to_sleep  = idle_ns;
            if (remaining < to_sleep)
                to_sleep = (long)remaining;
            sleep_ns(to_sleep);
        }
    }
}

static void *worker(void *arg)
{
    worker_args_t *wa = (worker_args_t *)arg;
    volatile double sink = 1.0; /* thread-local, avoids false sharing */

    for (size_t i = 0; i < wa->count; i++)
        run_instruction(&wa->instrs[i], &sink);

    return NULL;
}

/* Parse the CSV into a heap array of instructions. Returns count, or -1. */
static ssize_t parse_instructions(const char *path, instruction_t **out)
{
    FILE *f = fopen(path, "r");
    if (!f) {
        fprintf(stderr, "load: cannot open '%s': %s\n", path, strerror(errno));
        return -1;
    }

    size_t         cap = 16, n = 0;
    instruction_t *arr = malloc(cap * sizeof(*arr));
    if (!arr) {
        fprintf(stderr, "load: out of memory\n");
        fclose(f);
        return -1;
    }

    char   *line = NULL;
    size_t  len  = 0;
    ssize_t nread;
    long    lineno = 0;

    while ((nread = getline(&line, &len, f)) != -1) {
        lineno++;

        /* Skip blank lines and comments (# ...). */
        char *p = line;
        while (*p == ' ' || *p == '\t') p++;
        if (*p == '\0' || *p == '\n' || *p == '\r' || *p == '#')
            continue;

        long   t;
        double pct;
        if (sscanf(p, " %ld , %lf", &t, &pct) != 2) {
            fprintf(stderr, "load: skipping malformed line %ld: %s",
                    lineno, line);
            continue;
        }

        if (n == cap) {
            cap *= 2;
            instruction_t *tmp = realloc(arr, cap * sizeof(*arr));
            if (!tmp) {
                fprintf(stderr, "load: out of memory\n");
                free(arr);
                free(line);
                fclose(f);
                return -1;
            }
            arr = tmp;
        }

        arr[n].time_ms = t;
        arr[n].percent = pct;
        n++;
    }

    free(line);
    fclose(f);
    *out = arr;
    return (ssize_t)n;
}

int main(int argc, char **argv)
{
    if (argc != 3) {
        fprintf(stderr, "Usage: %s <N> <instructions_file.csv>\n", argv[0]);
        return 1;
    }

    char *endp = NULL;
    long  N    = strtol(argv[1], &endp, 10);
    if (*endp != '\0' || N < 1) {
        fprintf(stderr, "load: <N> must be a positive integer\n");
        return 1;
    }

    instruction_t *instrs = NULL;
    ssize_t        count  = parse_instructions(argv[2], &instrs);
    if (count < 0)
        return 1;
    if (count == 0) {
        fprintf(stderr, "load: no valid instructions in '%s'\n", argv[2]);
        free(instrs);
        return 1;
    }

    pthread_t     *threads = malloc((size_t)N * sizeof(*threads));
    worker_args_t *wargs   = malloc((size_t)N * sizeof(*wargs));
    if (!threads || !wargs) {
        fprintf(stderr, "load: out of memory\n");
        free(threads);
        free(wargs);
        free(instrs);
        return 1;
    }

    printf("load: %ld worker threads, %zd instructions\n", N, count);

    for (long i = 0; i < N; i++) {
        wargs[i].instrs = instrs;
        wargs[i].count  = (size_t)count;
        wargs[i].id     = (int)i;
        int rc = pthread_create(&threads[i], NULL, worker, &wargs[i]);
        if (rc != 0) {
            fprintf(stderr, "load: pthread_create failed: %s\n", strerror(rc));
            /* Join whatever we already started before bailing out. */
            for (long j = 0; j < i; j++)
                pthread_join(threads[j], NULL);
            free(threads);
            free(wargs);
            free(instrs);
            return 1;
        }
    }

    for (long i = 0; i < N; i++)
        pthread_join(threads[i], NULL);

    printf("load: done\n");

    free(threads);
    free(wargs);
    free(instrs);
    return 0;
}

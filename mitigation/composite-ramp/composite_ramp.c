#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdatomic.h>
#include <pthread.h>
#include <sched.h>
#include <unistd.h>
#include <time.h>
#include <math.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <errno.h>

#define MAX_CORES           128
#define CYCLE_US            10000
#define SAMPLE_INTERVAL_US  100000
#define AT_FULL_THRESHOLD   95.0
#define AT_ZERO_THRESHOLD   5.0

typedef struct {
    int            core;
    _Atomic double target;
    _Atomic int    stop;
    pthread_t      thread;
} Worker;

typedef struct {
    long long user, nice, system, idle, iowait, irq, softirq, steal;
} CpuStat;

typedef struct {
    double  ramp_rate;
    int     start_core;
    int     end_core;
    char   *command;
} Instruction;

/* ---- timespec helpers ---- */

static void ts_add_ns(struct timespec *ts, long long ns)
{
    ts->tv_nsec += ns;
    while (ts->tv_nsec >= 1000000000LL) {
        ts->tv_sec++;
        ts->tv_nsec -= 1000000000LL;
    }
}

static long long ts_diff_ns(const struct timespec *a, const struct timespec *b)
{
    return (a->tv_sec  - b->tv_sec)  * 1000000000LL
         + (a->tv_nsec - b->tv_nsec);
}

/* ---- worker thread ---- */

static void *worker_thread(void *arg)
{
    Worker *w = (Worker *)arg;

    cpu_set_t cpuset;
    CPU_ZERO(&cpuset);
    CPU_SET(w->core, &cpuset);
    int rc = pthread_setaffinity_np(pthread_self(), sizeof(cpuset), &cpuset);
    if (rc)
        fprintf(stderr, "warning: affinity for core %d failed: %s\n",
                w->core, strerror(rc));

    struct sched_param sp = { .sched_priority = 0 };
    rc = pthread_setschedparam(pthread_self(), SCHED_IDLE, &sp);
    if (rc)
        fprintf(stderr, "warning: SCHED_IDLE for core %d failed: %s\n",
                w->core, strerror(rc));

    struct timespec cycle_start;
    clock_gettime(CLOCK_MONOTONIC, &cycle_start);

    while (!atomic_load_explicit(&w->stop, memory_order_relaxed)) {
        double tgt      = atomic_load_explicit(&w->target, memory_order_relaxed);
        long long cycle = (long long)CYCLE_US * 1000LL;
        long long busy  = (long long)(tgt / 100.0 * cycle);

        struct timespec busy_end  = cycle_start;
        struct timespec cycle_end = cycle_start;
        ts_add_ns(&busy_end,  busy);
        ts_add_ns(&cycle_end, cycle);

        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        while (ts_diff_ns(&busy_end, &now) > 0) {
            volatile double x = 1.0;
            for (int i = 0; i < 1000; i++) x += 0.000001;
            clock_gettime(CLOCK_MONOTONIC, &now);
        }

        clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &cycle_end, NULL);
        cycle_start = cycle_end;
    }

    return NULL;
}

/* ---- /proc/stat ---- */

static int read_cpu_stat(int core, CpuStat *s)
{
    FILE *f = fopen("/proc/stat", "r");
    if (!f) { perror("fopen /proc/stat"); return -1; }

    char line[256], prefix[16];
    snprintf(prefix, sizeof(prefix), "cpu%d ", core);
    size_t plen = strlen(prefix);
    int found = 0;

    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, prefix, plen) == 0) {
            sscanf(line + plen, "%lld %lld %lld %lld %lld %lld %lld %lld",
                   &s->user, &s->nice, &s->system, &s->idle,
                   &s->iowait, &s->irq, &s->softirq, &s->steal);
            found = 1;
            break;
        }
    }
    fclose(f);
    if (!found) {
        fprintf(stderr, "cpu%d not found in /proc/stat\n", core);
        return -1;
    }
    return 0;
}

static double cpu_usage(const CpuStat *prev, const CpuStat *curr)
{
    long long prev_idle  = prev->idle + prev->iowait;
    long long curr_idle  = curr->idle + curr->iowait;
    long long prev_total = prev->user + prev->nice + prev->system + prev->idle
                         + prev->iowait + prev->irq + prev->softirq + prev->steal;
    long long curr_total = curr->user + curr->nice + curr->system + curr->idle
                         + curr->iowait + curr->irq + curr->softirq + curr->steal;
    long long dt = curr_total - prev_total;
    if (dt <= 0) return 0.0;
    return 100.0 * (1.0 - (double)(curr_idle - prev_idle) / dt);
}

/* ---- ramp helpers ---- */

static void ramp_core_up(Worker *w, double rate)
{
    CpuStat prev, curr;
    read_cpu_stat(w->core, &prev);

    for (;;) {
        usleep(SAMPLE_INTERVAL_US);
        double dt  = SAMPLE_INTERVAL_US / 1e6;
        double tgt = atomic_load_explicit(&w->target, memory_order_relaxed);
        if (tgt < 100.0) {
            tgt = fmin(tgt + rate * dt, 100.0);
            atomic_store_explicit(&w->target, tgt, memory_order_relaxed);
        }

        read_cpu_stat(w->core, &curr);
        double usage = cpu_usage(&prev, &curr);
        prev = curr;

        fprintf(stderr, "ramp-up   core %-2d  target %5.1f%%  actual %5.1f%%\n",
                w->core, tgt, usage);

        if (tgt >= 100.0 && usage >= AT_FULL_THRESHOLD)
            break;
    }
}

static void ramp_core_down(Worker *w, double rate)
{
    CpuStat prev, curr;
    read_cpu_stat(w->core, &prev);

    for (;;) {
        usleep(SAMPLE_INTERVAL_US);
        double dt  = SAMPLE_INTERVAL_US / 1e6;
        double tgt = atomic_load_explicit(&w->target, memory_order_relaxed);
        if (tgt > 0.0) {
            tgt = fmax(tgt - rate * dt, 0.0);
            atomic_store_explicit(&w->target, tgt, memory_order_relaxed);
        }

        read_cpu_stat(w->core, &curr);
        double usage = cpu_usage(&prev, &curr);
        prev = curr;

        fprintf(stderr, "ramp-down core %-2d  target %5.1f%%  actual %5.1f%%\n",
                w->core, tgt, usage);

        if (tgt <= 0.0 && usage <= AT_ZERO_THRESHOLD)
            break;
    }

    atomic_store_explicit(&w->stop, 1, memory_order_relaxed);
}

/* ---- command runner ---- */

static int run_command(const char *cmdstr, int *active_cores, int ncore)
{
    char *buf = strdup(cmdstr);
    char *argv_arr[256];
    int n = 0;
    for (char *p = strtok(buf, " \t"); p && n < 255; p = strtok(NULL, " \t"))
        argv_arr[n++] = p;
    argv_arr[n] = NULL;

    if (n == 0) {
        fprintf(stderr, "empty command\n");
        free(buf);
        return -1;
    }

    pid_t pid = fork();
    if (pid < 0) { perror("fork"); free(buf); return -1; }

    if (pid == 0) {
        cpu_set_t cpuset;
        CPU_ZERO(&cpuset);
        for (int i = 0; i < ncore; i++)
            CPU_SET(active_cores[i], &cpuset);
        sched_setaffinity(0, sizeof(cpuset), &cpuset);
        execvp(argv_arr[0], argv_arr);
        perror("execvp");
        _exit(127);
    }

    int status;
    while (waitpid(pid, &status, 0) < 0)
        if (errno != EINTR) { perror("waitpid"); free(buf); return -1; }

    free(buf);
    return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

/* ---- instruction parser ---- */

static void free_instructions(Instruction *arr, int n)
{
    for (int i = 0; i < n; i++)
        free(arr[i].command);
    free(arr);
}

static int parse_instructions(const char *path, Instruction **out, int *count)
{
    FILE *f = fopen(path, "r");
    if (!f) { perror("fopen"); return -1; }

    Instruction *arr = NULL;
    int n = 0, cap = 0;
    char line[1024];
    int lineno = 0;

    while (fgets(line, sizeof(line), f)) {
        lineno++;

        /* strip trailing whitespace/newline */
        int len = (int)strlen(line);
        while (len > 0 && (line[len-1] == '\n' || line[len-1] == '\r' ||
                            line[len-1] == ' '  || line[len-1] == '\t'))
            line[--len] = 0;
        if (len == 0)
            continue;

        char *comma1 = strchr(line, ',');
        if (!comma1) {
            fprintf(stderr, "line %d: missing first comma\n", lineno);
            fclose(f);
            free_instructions(arr, n);
            return -1;
        }
        *comma1 = 0;
        double ramp_rate = atof(line);

        char *comma2 = strchr(comma1 + 1, ',');
        if (!comma2) {
            fprintf(stderr, "line %d: missing second comma\n", lineno);
            fclose(f);
            free_instructions(arr, n);
            return -1;
        }
        *comma2 = 0;

        char *command = comma2 + 1;
        while (*command == ' ' || *command == '\t')
            command++;

        if (*command == 0) {
            fprintf(stderr, "line %d: empty command\n", lineno);
            fclose(f);
            free_instructions(arr, n);
            return -1;
        }

        /* parse core range "start:end"; ignored for the exit sentinel line */
        int start_core = 0, end_core = 0;
        if (strcmp(command, "exit") != 0) {
            char *range_str = comma1 + 1;
            char *colon = strchr(range_str, ':');
            if (!colon) {
                fprintf(stderr, "line %d: core range must be start:end (e.g. 0:7)\n", lineno);
                fclose(f);
                free_instructions(arr, n);
                return -1;
            }
            *colon = 0;
            start_core = atoi(range_str);
            end_core   = atoi(colon + 1);

            if (start_core < 0 || end_core < start_core || end_core >= MAX_CORES) {
                fprintf(stderr, "line %d: invalid core range %d:%d (must satisfy 0 <= start <= end < %d)\n",
                        lineno, start_core, end_core, MAX_CORES);
                fclose(f);
                free_instructions(arr, n);
                return -1;
            }
        }

        if (n >= cap) {
            cap = cap ? cap * 2 : 8;
            arr = realloc(arr, (size_t)cap * sizeof(Instruction));
        }
        arr[n].ramp_rate   = ramp_rate;
        arr[n].start_core  = start_core;
        arr[n].end_core    = end_core;
        arr[n].command     = strdup(command);
        n++;
    }
    fclose(f);

    *out   = arr;
    *count = n;
    return 0;
}

/* ---- main ---- */

int main(int argc, char *argv[])
{
    if (argc < 2) {
        fprintf(stderr, "usage: %s <instructions.csv>\n", argv[0]);
        return 1;
    }

    Instruction *instructions = NULL;
    int n_inst = 0;
    if (parse_instructions(argv[1], &instructions, &n_inst) < 0)
        return 1;

    if (n_inst < 2) {
        fprintf(stderr,
            "error: instructions file must contain at least 2 lines.\n"
            "  The last line must be the exit sentinel: <ramp_rate>,<anything>,exit\n"
            "  It specifies the ramp-down rate after all commands finish; without it\n"
            "  the program does not know how fast to ramp down at the end.\n");
        free_instructions(instructions, n_inst);
        return 1;
    }

    if (strcmp(instructions[n_inst - 1].command, "exit") != 0) {
        fprintf(stderr,
            "error: last line must be the exit sentinel: <ramp_rate>,<anything>,exit\n"
            "  Its ramp_rate is used for the final ramp-down; the core range is ignored.\n");
        free_instructions(instructions, n_inst);
        return 1;
    }

    Worker *workers[MAX_CORES];
    memset(workers, 0, sizeof(workers));
    int cur_start = -1, cur_end = -1;   /* no active range initially */

    for (int i = 0; i < n_inst - 1; i++) {   /* exclude exit sentinel */
        double rate      = instructions[i].ramp_rate;
        int    new_start = instructions[i].start_core;
        int    new_end   = instructions[i].end_core;

        if (cur_start == -1) {
            /* Initial ramp-up: gradual and sequential per the original spec. */
            for (int j = new_start; j <= new_end; j++) {
                workers[j] = malloc(sizeof(Worker));
                workers[j]->core = j;
                atomic_init(&workers[j]->target, 0.0);
                atomic_init(&workers[j]->stop,   0);
                pthread_create(&workers[j]->thread, NULL, worker_thread, workers[j]);
                ramp_core_up(workers[j], rate);
            }
        } else {
            /* Collect cores leaving and entering the active set. */
            int to_add[MAX_CORES], n_add = 0;
            for (int j = new_start; j <= new_end; j++)
                if (j < cur_start || j > cur_end)
                    to_add[n_add++] = j;               /* ascending order */

            int to_remove[MAX_CORES], n_remove = 0;
            for (int j = cur_end; j >= cur_start; j--)  /* LIFO: highest first */
                if (j < new_start || j > new_end)
                    to_remove[n_remove++] = j;

            int n_immediate = (n_remove < n_add) ? n_remove : n_add;

            /*
             * 1-for-1 replacements: stop each outgoing core and immediately
             * spin its replacement up to 100%, keeping total usage flat.
             */
            for (int i = 0; i < n_immediate; i++)
                atomic_store_explicit(&workers[to_remove[i]]->stop, 1, memory_order_relaxed);
            for (int i = 0; i < n_immediate; i++) {
                int j = to_add[i];
                workers[j] = malloc(sizeof(Worker));
                workers[j]->core = j;
                atomic_init(&workers[j]->target, 100.0);
                atomic_init(&workers[j]->stop,   0);
                pthread_create(&workers[j]->thread, NULL, worker_thread, workers[j]);
            }
            for (int i = 0; i < n_immediate; i++) {
                pthread_join(workers[to_remove[i]]->thread, NULL);
                free(workers[to_remove[i]]);
                workers[to_remove[i]] = NULL;
            }
            if (n_immediate > 0) {
                fprintf(stderr, "swapped: stopped");
                for (int i = 0; i < n_immediate; i++)
                    fprintf(stderr, " %d", to_remove[i]);
                fprintf(stderr, "  started");
                for (int i = 0; i < n_immediate; i++)
                    fprintf(stderr, " %d", to_add[i]);
                fprintf(stderr, "\n");
            }

            /* Ramp down excess old cores sequentially (LIFO). */
            for (int i = n_immediate; i < n_remove; i++) {
                ramp_core_down(workers[to_remove[i]], rate);
                pthread_join(workers[to_remove[i]]->thread, NULL);
                free(workers[to_remove[i]]);
                workers[to_remove[i]] = NULL;
            }

            /* Ramp up any net-new cores sequentially at the given rate. */
            for (int i = n_immediate; i < n_add; i++) {
                int j = to_add[i];
                workers[j] = malloc(sizeof(Worker));
                workers[j]->core = j;
                atomic_init(&workers[j]->target, 0.0);
                atomic_init(&workers[j]->stop,   0);
                pthread_create(&workers[j]->thread, NULL, worker_thread, workers[j]);
                ramp_core_up(workers[j], rate);
            }
        }
        cur_start = new_start;
        cur_end   = new_end;

        int active_cores[MAX_CORES];
        int ncore = 0;
        for (int j = cur_start; j <= cur_end; j++)
            active_cores[ncore++] = j;

        fprintf(stderr, "cores %d:%d ready — launching: %s\n",
                cur_start, cur_end, instructions[i].command);
        int ret = run_command(instructions[i].command, active_cores, ncore);
        fprintf(stderr, "command exited (%d)\n", ret);
    }

    /* final ramp-down LIFO at the last instruction's ramp_rate */
    double final_rate = instructions[n_inst - 1].ramp_rate;
    fprintf(stderr, "ramping down cores %d:%d at %.1f%%/s\n",
            cur_start, cur_end, final_rate);
    for (int j = cur_end; j >= cur_start; j--) {
        ramp_core_down(workers[j], final_rate);
        pthread_join(workers[j]->thread, NULL);
        free(workers[j]);
        workers[j] = NULL;
    }

    free_instructions(instructions, n_inst);
    return 0;
}

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
#include <sys/stat.h>
#include <fcntl.h>
#include <errno.h>

#define MAX_CORES           64
#define CYCLE_US            10000       /* worker busy/sleep cycle (10 ms) */
#define SAMPLE_INTERVAL_US  100000      /* /proc/stat polling interval (100 ms) */
#define AT_FULL_THRESHOLD   95.0        /* % to consider a core fully loaded */
#define AT_ZERO_THRESHOLD   5.0         /* % to consider a core idle */
#define RISK_POLL_US        100000      /* risk-file polling cadence (100 ms) */
#define RISK_MAX_AGE_S      15.0        /* stale detector flag = fail open */
#define USAGE_EDGE_POLL_US  100000      /* usage-edge polling cadence (100 ms) */
#define USAGE_EDGE_RISE_PS  25.0        /* default aggregate usage rise, %/s */

typedef struct {
    int            core;
    _Atomic double target;
    _Atomic int    stop;
    pthread_t      thread;
} Worker;

typedef struct {
    long long user, nice, system, idle, iowait, irq, softirq, steal;
} CpuStat;

/* ---- timespec helpers ---- */

static void ts_add_ns(struct timespec *ts, long long ns)
{
    ts->tv_nsec += ns;
    while (ts->tv_nsec >= 1000000000LL) {
        ts->tv_sec++;
        ts->tv_nsec -= 1000000000LL;
    }
}

/* returns a - b in nanoseconds */
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

        /* busy spin until busy_end */
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        while (ts_diff_ns(&busy_end, &now) > 0) {
            volatile double x = 1.0;
            for (int i = 0; i < 1000; i++) x += 0.000001;
            clock_gettime(CLOCK_MONOTONIC, &now);
        }

        /* sleep for remainder of cycle */
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

static int read_total_cpu_stat(CpuStat *s)
{
    FILE *f = fopen("/proc/stat", "r");
    if (!f) { perror("fopen /proc/stat"); return -1; }

    char line[256];
    int ok = 0;
    if (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "cpu ", 4) == 0) {
            sscanf(line + 4, "%lld %lld %lld %lld %lld %lld %lld %lld",
                   &s->user, &s->nice, &s->system, &s->idle,
                   &s->iowait, &s->irq, &s->softirq, &s->steal);
            ok = 1;
        }
    }
    fclose(f);
    if (!ok) {
        fprintf(stderr, "aggregate cpu line not found in /proc/stat\n");
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

static int run_command(char **cmd, int *cores, int ncore)
{
    pid_t pid = fork();
    if (pid < 0) { perror("fork"); return -1; }

    if (pid == 0) {
        cpu_set_t cpuset;
        CPU_ZERO(&cpuset);
        for (int i = 0; i < ncore; i++)
            CPU_SET(cores[i], &cpuset);
        sched_setaffinity(0, sizeof(cpuset), &cpuset);
        execvp(cmd[0], cmd);
        perror("execvp");
        _exit(127);
    }

    int status;
    while (waitpid(pid, &status, 0) < 0)
        if (errno != EINTR) { perror("waitpid"); return -1; }

    return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

/* ---- core list parser ---- */

static int parse_cores(const char *str, int *out, int max)
{
    char *buf = strdup(str);
    int n = 0;
    for (char *p = strtok(buf, ","); p && n < max; p = strtok(NULL, ","))
        out[n++] = atoi(p);
    free(buf);
    return n;
}

/* ---- risk flag watcher ---- */

static void usage(const char *prog)
{
    fprintf(stderr,
            "usage: %s <cores> <ramp_up_%%/s> <ramp_down_%%/s> "
            "[--risk-file PATH] [--flag-max-age S] "
            "[--usage-edge PCT] [--usage-rise PCT_PER_S] <cmd> [args...]\n"
            "       %s --selfcheck\n"
            "  cores: comma-separated CPU list, e.g. 0,2,4\n",
            prog, prog);
}

static double file_age_s(const struct stat *st)
{
    struct timespec now;
    clock_gettime(CLOCK_REALTIME, &now);
    return (double)(now.tv_sec - st->st_mtim.tv_sec)
         + (double)(now.tv_nsec - st->st_mtim.tv_nsec) / 1e9;
}

static int read_risk_flag(const char *path, double max_age_s, int *risk)
{
    struct stat st;
    if (stat(path, &st) != 0)
        return -1;
    if (file_age_s(&st) > max_age_s)
        return -1;

    int fd = open(path, O_RDONLY);
    if (fd < 0)
        return -1;

    char buf[64];
    ssize_t n = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (n <= 0)
        return -1;
    buf[n] = '\0';

    char last = '\0';
    for (ssize_t i = 0; i < n; i++)
        if (buf[i] != '\n' && buf[i] != '\r' && buf[i] != ' ' && buf[i] != '\t')
            last = buf[i];
    if (last == '0' || last == '1') {
        *risk = (last == '1');
        return 0;
    }
    return -1;
}

static int wait_for_risk_edge(const char *path, double max_age_s)
{
    int prev = 0;  /* fresh initial "1" counts as an unhandled 0->1 edge */

    for (;;) {
        int cur = 0;
        if (read_risk_flag(path, max_age_s, &cur) != 0) {
            fprintf(stderr,
                    "warning: risk file unavailable/stale; proceeding immediately\n");
            return 0;
        }
        if (cur && !prev) {
            fprintf(stderr, "risk edge: fresh 0->1 flag observed; starting ramp-up\n");
            return 1;
        }
        prev = cur;
        usleep(RISK_POLL_US);
    }
}

static int usage_edge_fired(double prev_usage, double usage, double dt_s,
                            double threshold, double min_rise_per_s)
{
    if (dt_s <= 0.0)
        return 0;
    if (usage < threshold)
        return 0;
    if (prev_usage < threshold)
        return 1;
    return ((usage - prev_usage) / dt_s) >= min_rise_per_s;
}

static int wait_for_usage_edge(double threshold, double min_rise_per_s)
{
    CpuStat prev_stat, curr_stat;
    if (read_total_cpu_stat(&prev_stat) != 0)
        return -1;

    usleep(USAGE_EDGE_POLL_US);
    if (read_total_cpu_stat(&curr_stat) != 0)
        return -1;
    double dt = USAGE_EDGE_POLL_US / 1e6;
    double prev_usage = cpu_usage(&prev_stat, &curr_stat);
    prev_stat = curr_stat;

    for (;;) {
        usleep(USAGE_EDGE_POLL_US);
        if (read_total_cpu_stat(&curr_stat) != 0)
            return -1;
        double usage = cpu_usage(&prev_stat, &curr_stat);
        if (usage_edge_fired(prev_usage, usage, dt, threshold, min_rise_per_s)) {
            fprintf(stderr,
                    "usage edge: aggregate CPU %.1f%% after %.1f%%; starting ramp-up\n",
                    usage, prev_usage);
            return 1;
        }
        prev_usage = usage;
        prev_stat = curr_stat;
    }
}

static int write_text_file(const char *path, const char *text)
{
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0)
        return -1;
    size_t n = strlen(text);
    ssize_t w = write(fd, text, n);
    close(fd);
    return (w == (ssize_t)n) ? 0 : -1;
}

static int selfcheck(void)
{
    char path[] = "/tmp/ramp_risk_XXXXXX";
    int fd = mkstemp(path);
    if (fd < 0) {
        perror("mkstemp");
        return 1;
    }
    if (write(fd, "0\n", 2) != 2) {
        perror("write");
        close(fd);
        unlink(path);
        return 1;
    }
    close(fd);

    pid_t pid = fork();
    if (pid < 0) {
        perror("fork");
        unlink(path);
        return 1;
    }
    if (pid == 0) {
        usleep(250000);
        _exit(write_text_file(path, "1\n") == 0 ? 0 : 1);
    }

    alarm(5);
    int edge = wait_for_risk_edge(path, RISK_MAX_AGE_S);
    alarm(0);

    int status = 0;
    if (waitpid(pid, &status, 0) < 0) {
        perror("waitpid");
        unlink(path);
        return 1;
    }
    unlink(path);

    int missing_fails_open = (wait_for_risk_edge(path, RISK_MAX_AGE_S) == 0);
    int usage_low = !usage_edge_fired(2.0, 4.0, 0.1, 10.0, USAGE_EDGE_RISE_PS);
    int usage_cross = usage_edge_fired(2.0, 12.0, 0.1, 10.0, USAGE_EDGE_RISE_PS);
    int usage_steady = !usage_edge_fired(20.0, 21.0, 0.1, 10.0, USAGE_EDGE_RISE_PS);
    if (edge == 1 && WIFEXITED(status) && WEXITSTATUS(status) == 0
            && missing_fails_open && usage_low && usage_cross && usage_steady) {
        printf("selfcheck OK: risk-file 0->1 edge, stale/missing fail-open, usage edge\n");
        return 0;
    }
    fprintf(stderr, "selfcheck FAILED: edge=%d child_status=%d fail_open=%d "
            "usage_low=%d usage_cross=%d usage_steady=%d\n",
            edge, status, missing_fails_open, usage_low, usage_cross, usage_steady);
    return 1;
}

/* ---- main ---- */

int main(int argc, char *argv[])
{
    if (argc == 2 && strcmp(argv[1], "--selfcheck") == 0)
        return selfcheck();

    const char *risk_file = NULL;
    double flag_max_age = RISK_MAX_AGE_S;
    int usage_edge_enabled = 0;
    double usage_edge_threshold = 0.0;
    double usage_edge_rise = USAGE_EDGE_RISE_PS;
    const char *pos[3] = {0};
    int npos = 0;
    int argi = 1;

    while (argi < argc) {
        if (strcmp(argv[argi], "--risk-file") == 0) {
            if (argi + 1 >= argc) {
                usage(argv[0]);
                return 1;
            }
            risk_file = argv[argi + 1];
            argi += 2;
            continue;
        }
        if (strcmp(argv[argi], "--flag-max-age") == 0) {
            if (argi + 1 >= argc) {
                usage(argv[0]);
                return 1;
            }
            flag_max_age = atof(argv[argi + 1]);
            argi += 2;
            continue;
        }
        if (strcmp(argv[argi], "--usage-edge") == 0) {
            if (argi + 1 >= argc) {
                usage(argv[0]);
                return 1;
            }
            usage_edge_threshold = atof(argv[argi + 1]);
            usage_edge_enabled = 1;
            argi += 2;
            continue;
        }
        if (strcmp(argv[argi], "--usage-rise") == 0) {
            if (argi + 1 >= argc) {
                usage(argv[0]);
                return 1;
            }
            usage_edge_rise = atof(argv[argi + 1]);
            argi += 2;
            continue;
        }
        if (npos < 3) {
            pos[npos++] = argv[argi++];
            continue;
        }
        break;
    }

    if (npos < 3 || argi >= argc || flag_max_age <= 0.0
            || usage_edge_threshold < 0.0 || usage_edge_threshold > 100.0
            || usage_edge_rise < 0.0) {
        usage(argv[0]);
        return 1;
    }
    if (risk_file && usage_edge_enabled) {
        fprintf(stderr, "choose only one trigger: --risk-file or --usage-edge\n");
        return 1;
    }

    int cores[MAX_CORES];
    int ncore = parse_cores(pos[0], cores, MAX_CORES);
    if (ncore <= 0) { fprintf(stderr, "no cores specified\n"); return 1; }

    double ramp_up   = atof(pos[1]);
    double ramp_down = atof(pos[2]);
    char **cmd       = &argv[argi];

    Worker workers[MAX_CORES];
    for (int i = 0; i < ncore; i++) {
        workers[i].core = cores[i];
        atomic_init(&workers[i].target, 0.0);
        atomic_init(&workers[i].stop,   0);
        pthread_create(&workers[i].thread, NULL, worker_thread, &workers[i]);
    }

    if (risk_file)
        wait_for_risk_edge(risk_file, flag_max_age);
    if (usage_edge_enabled && wait_for_usage_edge(usage_edge_threshold,
                                                  usage_edge_rise) != 1)
        return 1;

    for (int i = 0; i < ncore; i++)
        ramp_core_up(&workers[i], ramp_up);

    fprintf(stderr, "all cores loaded — launching: %s\n", cmd[0]);
    int ret = run_command(cmd, cores, ncore);
    fprintf(stderr, "command exited (%d) — ramping down\n", ret);

    for (int i = 0; i < ncore; i++)
        ramp_core_down(&workers[i], ramp_down);

    for (int i = 0; i < ncore; i++)
        pthread_join(workers[i].thread, NULL);

    return ret;
}

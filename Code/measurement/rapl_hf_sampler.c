/* rapl_hf_sampler — one tiny C daemon for the two 10 Hz hot loops.
 *
 * Replaces the RAPL half of influx2.py (cpu_power + run_queue at --rapl-hz)
 * and all of Model/analysis/hf_edge_scorer.py (run-queue edge score with
 * adaptive rank threshold + refractory). Motivation: the Python versions were
 * two interpreters waking 10x/s, one np.nanquantile(18k) per sample, and up
 * to 12 HTTP POSTs/s between them — measurable package-power overhead on the
 * very signal under study (Model/analysis/spike_daemon_overhead_notes.md).
 * This daemon is a single process, ~zero allocations at steady state, and one
 * plain-HTTP keep-alive POST per flush window (immediate on a risk edge).
 *
 * Parity contracts (do not break silently):
 *   - watts math == influx2.py RaplAccumulator / combine_socket_watts /
 *     socket_deltas: per-socket wraparound, zero-delta accumulation (no false
 *     spikes at fast polling), total emitted only once every socket reported.
 *   - edge score == hf_edge_scorer.py: rises over 3 s / 5 s lags on a 10 Hz
 *     grid, threshold = max(20, q0.995 of last 18000 scores, warmup 600),
 *     5 s refractory, threshold recomputed 1x/s, score appended AFTER the
 *     decision. Field names/types match the Python writers exactly.
 *   - single writer per measurement: run this OR (influx2.py without
 *     --no-rapl, hf-edge-scorer.service) — never both sides at once.
 *
 * Build:      cc -O2 -Wall -o rapl_hf_sampler rapl_hf_sampler.c -lm
 * Selfcheck:  ./rapl_hf_sampler --selfcheck        (no root, no network)
 * Dry run:    ./rapl_hf_sampler --dry-run          (prints line protocol)
 * Live:       sudo ./rapl_hf_sampler               (root for energy_uj)
 *
 * Secrets: reads ~/.secrets/influx_org.txt + influx_token.txt (0600). The org
 * name and token are never logged (repo guardrail).
 */
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <math.h>
#include <netdb.h>
#include <pwd.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

/* ---- config (mirrors the Python constants) ---- */
#define SAMPLE_HZ        10.0
#define EDGE_WRITE_HZ     2.0
#define FLUSH_S           5.0
#define LAG_A            30      /* 3 s on the 10 Hz grid */
#define LAG_B            50      /* 5 s */
#define NSAMP            (LAG_B + 1)
#define MIN_EDGE_DELTA   20.0
#define FLAG_BUDGET      0.005   /* top 0.5% of live edge scores */
#define SCORE_WINDOW     18000   /* ~30 min at 10 Hz */
#define MIN_RANK_SCORES  600     /* ~1 min warm-up */
#define REFRACTORY_S     5.0
#define THR_REFRESH_S    1.0
#define MAX_SOCKETS      8
#define OUTBUF_CAP       (512 * 1024)
#define HTTP_TIMEOUT_S   5

/* 1 Hz per-core usage/freq/temp (replaces influx2.py's psutil half) */
#define CPUSTATS_S        1.0
#define MAX_CORES        512
#define MAX_TEMPS        256

#define EDGE_MEASUREMENT "power_prediction_pathb_hf_edge"

static const char *DEFAULT_URL = "http://mycroft:8086";
static const char *BUCKET = "Power";

/* ---------------- pure logic (selfchecked) ---------------- */

/* socket_deltas(): per-socket wraparound */
static uint64_t energy_delta(uint64_t prev, uint64_t cur, uint64_t max_range) {
    return cur >= prev ? cur - prev : cur + max_range - prev;
}

/* RaplAccumulator.add(): zero-delta accumulation, watts only on advance.
 * MIN_EMIT_S extension (2026-07-15): when the loop falls behind under load
 * it fires catch-up ticks back-to-back (dt ~sub-ms); if the RAPL counter
 * commits its ~1ms update quantum between two such reads, quantum/dt gives a
 * 1000+ W false spike (live: 1598 W single sample, 0.4 ms after its 231 W
 * neighbor). A window shorter than MIN_EMIT_S is treated like a zero-delta
 * poll: energy+time accumulate into the next emission, so the average over
 * the merged window stays exact and no energy is lost. Normal 10 Hz ticks
 * (0.1 s >= MIN_EMIT_S) emit exactly as before. */
#define MIN_EMIT_S 0.05   /* half the sample period */

typedef struct { uint64_t acc_uj; double acc_s; } Accum;

static double accum_add(Accum *a, uint64_t delta_uj, double dt_s) {
    a->acc_uj += delta_uj;
    a->acc_s += dt_s;
    if (delta_uj > 0 && a->acc_s >= MIN_EMIT_S) {
        double watts = ((double)a->acc_uj / 1e6) / a->acc_s;
        a->acc_uj = 0;
        a->acc_s = 0.0;
        return watts;
    }
    return -1.0;   /* None */
}

/* combine_socket_watts(): sum of latest per-socket watts once all reported */
static double combine_watts(const uint64_t *deltas, double dt, Accum *accs,
                            double *last_watts, int n) {
    for (int i = 0; i < n; i++) {
        double w = accum_add(&accs[i], deltas[i], dt);
        if (w >= 0) last_watts[i] = w;
    }
    double total = 0;
    for (int i = 0; i < n; i++) {
        if (last_watts[i] < 0) return -1.0;
        total += last_watts[i];
    }
    return total;
}

/* edge_score(): causal max rise over the two lags; needs a full window */
typedef struct { int ring[NSAMP]; int n, head; } Samples;

static void samples_push(Samples *s, int v) {
    s->ring[s->head] = v;
    s->head = (s->head + 1) % NSAMP;
    if (s->n < NSAMP) s->n++;
}

static int samples_back(const Samples *s, int back) {  /* back=0 -> newest */
    return s->ring[(s->head - 1 - back + 2 * NSAMP) % NSAMP];
}

static double edge_score(const Samples *s) {
    if (s->n < NSAMP) return -1.0;   /* None */
    double cur = samples_back(s, 0);
    double ra = cur - samples_back(s, LAG_A);
    double rb = cur - samples_back(s, LAG_B);
    double r = ra > rb ? ra : rb;
    return r > 0 ? r : 0.0;
}

/* rolling score window + numpy-'linear' interpolated quantile */
typedef struct { double *v; int n, head; double *scratch; } Scores;

static void scores_push(Scores *s, double x) {
    s->v[s->head] = x;
    s->head = (s->head + 1) % SCORE_WINDOW;
    if (s->n < SCORE_WINDOW) s->n++;
}

static int cmp_dbl(const void *a, const void *b) {
    double d = *(const double *)a - *(const double *)b;
    return d < 0 ? -1 : d > 0 ? 1 : 0;
}

static double quantile_linear(double *sorted, int n, double q) {
    if (n == 1) return sorted[0];
    double h = (n - 1) * q;
    int lo = (int)h;
    if (lo >= n - 1) return sorted[n - 1];
    return sorted[lo] + (h - lo) * (sorted[lo + 1] - sorted[lo]);
}

/* live_threshold(): conservative adaptive rank threshold */
static double live_threshold(Scores *s) {
    if (s->n < MIN_RANK_SCORES) return MIN_EDGE_DELTA;
    memcpy(s->scratch, s->v, (size_t)s->n * sizeof(double));
    qsort(s->scratch, (size_t)s->n, sizeof(double), cmp_dbl);
    double q = quantile_linear(s->scratch, s->n, 1.0 - FLAG_BUDGET);
    return q > MIN_EDGE_DELTA ? q : MIN_EDGE_DELTA;
}

/* line-protocol appenders (types must match the Python Point writers) */
static int lp_power(char *dst, size_t cap, const char *server, double watts,
                    int64_t ts_ns) {
    return snprintf(dst, cap, "cpu_power,server=%s watts=%.3f %lld\n",
                    server, watts, (long long)ts_ns);
}

static int lp_runq(char *dst, size_t cap, const char *server, int nr,
                   int64_t ts_ns) {
    return snprintf(dst, cap, "run_queue,server=%s nr_running=%di %lld\n",
                    server, nr, (long long)ts_ns);
}

static int lp_edge(char *dst, size_t cap, const char *server, int nr,
                   double score, double thr, int risk, int rank_n,
                   int64_t ts_ns) {
    return snprintf(dst, cap,
                    "%s,server=%s nr_running=%di,edge_score=%.3f,"
                    "edge_threshold=%.3f,spike_risk=%di,rank_buffer_n=%di %lld\n",
                    EDGE_MEASUREMENT, server, nr, score, thr, risk, rank_n,
                    (long long)ts_ns);
}

static int lp_usage(char *dst, size_t cap, const char *server, int core,
                    double pct, int64_t ts_ns) {
    return snprintf(dst, cap, "cpu_usage,server=%s,core=%d percent=%.3f %lld\n",
                    server, core, pct, (long long)ts_ns);
}

static int lp_freq(char *dst, size_t cap, const char *server, int core,
                   double mhz, int64_t ts_ns) {
    return snprintf(dst, cap, "cpu_freq,server=%s,core=%d mhz=%.3f %lld\n",
                    server, core, mhz, (long long)ts_ns);
}

static int lp_temp(char *dst, size_t cap, const char *server, const char *tags,
                   double celsius, int64_t ts_ns) {
    return snprintf(dst, cap, "cpu_temp,server=%s,%s celsius=%.1f %lld\n",
                    server, tags, celsius, (long long)ts_ns);
}

/* /proc/stat "cpuN u n s i io irq sirq steal": busy = total - idle - iowait
 * (psutil cpu_percent parity) */
static int parse_cpu_line(const char *line, int *cpu, uint64_t *busy,
                          uint64_t *total) {
    unsigned long long v[8] = {0};
    int n = sscanf(line, "cpu%d %llu %llu %llu %llu %llu %llu %llu %llu",
                   cpu, &v[0], &v[1], &v[2], &v[3], &v[4], &v[5], &v[6], &v[7]);
    if (n < 5) return 0;
    uint64_t tot = 0;
    for (int i = 0; i < 8; i++) tot += v[i];
    *total = tot;
    *busy = tot - v[3] - v[4];
    return 1;
}

static double cpu_percent(uint64_t dbusy, uint64_t dtotal) {
    if (dtotal == 0) return -1.0;
    double p = 100.0 * (double)dbusy / (double)dtotal;
    return p < 0.0 ? 0.0 : (p > 100.0 ? 100.0 : p);
}

/* ---------------- I/O helpers ---------------- */

static void die(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    exit(1);
}

static int64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

static double now_mono(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static char *read_trimmed(const char *path, char *buf, size_t cap) {
    FILE *f = fopen(path, "r");
    if (!f) return NULL;
    if (!fgets(buf, (int)cap, f)) { fclose(f); return NULL; }
    fclose(f);
    buf[strcspn(buf, " \t\r\n")] = 0;
    return buf;
}

static int read_nr_running(void) {   /* 4th loadavg field, "R/T" -> R */
    FILE *f = fopen("/proc/loadavg", "r");
    if (!f) return -1;
    double a, b, c;
    int r = -1, t;
    if (fscanf(f, "%lf %lf %lf %d/%d", &a, &b, &c, &r, &t) != 5) r = -1;
    fclose(f);
    return r;
}

static uint64_t read_u64_fd(int fd) {
    char buf[32];
    ssize_t n = pread(fd, buf, sizeof buf - 1, 0);
    if (n <= 0) return UINT64_MAX;
    buf[n] = 0;
    return strtoull(buf, NULL, 10);
}

/* ---- per-core stats collectors (influx2.py psutil-half parity) ---- */

typedef struct {
    int ncpu;                          /* 0 until first baseline read */
    uint64_t busy[MAX_CORES], total[MAX_CORES];
    int ntemp;
    int temp_fd[MAX_TEMPS];
    char temp_tags[MAX_TEMPS][96];     /* preformatted, e.g. "core=7" */
} CpuStats;

static char *read_label(const char *path, char *buf, size_t cap) {
    /* like read_trimmed but keeps interior spaces ("Core 7") */
    FILE *f = fopen(path, "r");
    if (!f) return NULL;
    if (!fgets(buf, (int)cap, f)) { fclose(f); return NULL; }
    fclose(f);
    buf[strcspn(buf, "\r\n")] = 0;
    return buf;
}

static int read_cpu_times(uint64_t *busy, uint64_t *total, int cap) {
    FILE *f = fopen("/proc/stat", "r");
    if (!f) return 0;
    char line[512];
    int ncpu = 0;
    while (fgets(line, sizeof line, f)) {
        if (strncmp(line, "cpu", 3) != 0 || line[3] == ' ') continue;
        int cpu; uint64_t b, t;
        if (parse_cpu_line(line, &cpu, &b, &t) && cpu >= 0 && cpu < cap) {
            busy[cpu] = b;
            total[cpu] = t;
            if (cpu + 1 > ncpu) ncpu = cpu + 1;
        }
    }
    fclose(f);
    return ncpu;
}

/* coretemp: "Core N" labels -> core=N tag (Intel); k10temp: labeled readings
 * with sensor/label tags (AMD) — same tag shapes influx2.py wrote. */
static void discover_temps(CpuStats *cs) {
    cs->ntemp = 0;
    glob_t g;
    if (glob("/sys/class/hwmon/hwmon*/name", GLOB_NOSORT, NULL, &g) != 0) return;
    for (size_t i = 0; i < g.gl_pathc && cs->ntemp < MAX_TEMPS; i++) {
        char name[64], base[256];
        if (!read_trimmed(g.gl_pathv[i], name, sizeof name)) continue;
        int is_core = !strcmp(name, "coretemp"), is_k10 = !strcmp(name, "k10temp");
        if (!is_core && !is_k10) continue;
        snprintf(base, sizeof base, "%.*s",
                 (int)(strrchr(g.gl_pathv[i], '/') - g.gl_pathv[i]), g.gl_pathv[i]);
        char pat[300];
        snprintf(pat, sizeof pat, "%s/temp*_label", base);
        glob_t gl;
        if (glob(pat, GLOB_NOSORT, NULL, &gl) != 0) continue;
        for (size_t j = 0; j < gl.gl_pathc && cs->ntemp < MAX_TEMPS; j++) {
            char label[64], tag[96];
            if (!read_label(gl.gl_pathv[j], label, sizeof label)) continue;
            int corenum;
            if (is_core) {
                if (sscanf(label, "Core %d", &corenum) != 1) continue;
                snprintf(tag, sizeof tag, "core=%d", corenum);
            } else {
                char esc[64];
                size_t k = 0;
                for (const char *p = label; *p && k < sizeof esc - 2; p++) {
                    if (*p == ' ' || *p == ',' || *p == '=') esc[k++] = '\\';
                    esc[k++] = *p;
                }
                esc[k] = 0;
                snprintf(tag, sizeof tag, "sensor=k10temp,label=%s", esc);
            }
            char inp[300];
            size_t len = strlen(gl.gl_pathv[j]);
            snprintf(inp, sizeof inp, "%.*s_input", (int)(len - 6), gl.gl_pathv[j]);
            int fd = open(inp, O_RDONLY);
            if (fd < 0) continue;
            cs->temp_fd[cs->ntemp] = fd;
            snprintf(cs->temp_tags[cs->ntemp], sizeof cs->temp_tags[0], "%s", tag);
            cs->ntemp++;
        }
        globfree(&gl);
    }
    globfree(&g);
}

static void emit_freqs(const char *server, int64_t ts_ns, char *out,
                       size_t *out_len) {
    /* /proc/cpuinfo "cpu MHz" — works without a cpufreq scaling driver
     * (mycroft has none), same source psutil falls back to */
    FILE *f = fopen("/proc/cpuinfo", "r");
    if (!f) return;
    char line[256];
    int core = 0;
    double mhz;
    while (fgets(line, sizeof line, f)) {
        if (sscanf(line, "cpu MHz : %lf", &mhz) == 1) {
            if (*out_len + 96 < OUTBUF_CAP)
                *out_len += (size_t)lp_freq(out + *out_len, OUTBUF_CAP - *out_len,
                                            server, core, mhz, ts_ns);
            core++;
        }
    }
    fclose(f);
}

static void emit_cpu_stats(CpuStats *cs, const char *server, int64_t ts_ns,
                           char *out, size_t *out_len) {
    static uint64_t busy[MAX_CORES], total[MAX_CORES];
    int ncpu = read_cpu_times(busy, total, MAX_CORES);
    for (int i = 0; i < ncpu && i < cs->ncpu; i++) {   /* skipped on baseline */
        double pct = cpu_percent(busy[i] - cs->busy[i], total[i] - cs->total[i]);
        if (pct >= 0 && *out_len + 96 < OUTBUF_CAP)
            *out_len += (size_t)lp_usage(out + *out_len, OUTBUF_CAP - *out_len,
                                         server, i, pct, ts_ns);
    }
    memcpy(cs->busy, busy, sizeof busy);
    memcpy(cs->total, total, sizeof total);
    cs->ncpu = ncpu;
    emit_freqs(server, ts_ns, out, out_len);
    for (int i = 0; i < cs->ntemp; i++) {
        uint64_t mdeg = read_u64_fd(cs->temp_fd[i]);
        if (mdeg == UINT64_MAX) continue;
        if (*out_len + 96 < OUTBUF_CAP)
            *out_len += (size_t)lp_temp(out + *out_len, OUTBUF_CAP - *out_len,
                                        server, cs->temp_tags[i],
                                        mdeg / 1000.0, ts_ns);
    }
}

/* ---- 1 Hz system/mem/disk/net upstream collectors (sys_influx.py parity
 * on the fields the RF models consume; per-device/iface tags collapsed to
 * sums — query_upstream means across tags anyway) ---- */

typedef struct {
    int primed;
    double t_prev;
    uint64_t ctxt_prev, rd_sect_prev, wr_sect_prev, rx_prev, tx_prev;
} UpStats;

static double rate_per_s(uint64_t prev, uint64_t cur, double dt) {
    return (dt > 0 && cur >= prev) ? (double)(cur - prev) / dt : -1.0;
}

static void emit_upstream(UpStats *u, const char *server, int64_t ts_ns,
                          char *out, size_t *out_len) {
    double l1 = 0, l5 = 0, l15 = 0;
    int procs_r = -1;
    uint64_t ctxt = 0, rd = 0, wr = 0, rx = 0, tx = 0;
    char line[512];

    FILE *f = fopen("/proc/loadavg", "r");
    if (f) {
        int tot;
        if (fscanf(f, "%lf %lf %lf %d/%d", &l1, &l5, &l15, &procs_r, &tot) != 5)
            procs_r = -1;
        fclose(f);
    }
    f = fopen("/proc/stat", "r");
    if (f) {
        while (fgets(line, sizeof line, f))
            if (sscanf(line, "ctxt %llu", (unsigned long long *)&ctxt) == 1) break;
        fclose(f);
    }
    uint64_t mem_total = 0, mem_avail = 0, cached = 0, swap_tot = 0, swap_free = 0;
    f = fopen("/proc/meminfo", "r");
    if (f) {
        unsigned long long v;
        while (fgets(line, sizeof line, f)) {
            if (sscanf(line, "MemTotal: %llu", &v) == 1) mem_total = v << 10;
            else if (sscanf(line, "MemAvailable: %llu", &v) == 1) mem_avail = v << 10;
            else if (sscanf(line, "Cached: %llu", &v) == 1) cached = v << 10;
            else if (sscanf(line, "SwapTotal: %llu", &v) == 1) swap_tot = v << 10;
            else if (sscanf(line, "SwapFree: %llu", &v) == 1) swap_free = v << 10;
        }
        fclose(f);
    }
    f = fopen("/proc/diskstats", "r");
    if (f) {
        while (fgets(line, sizeof line, f)) {
            char dev[32];
            unsigned long long rios, rmerge, rsect, rms, wios, wmerge, wsect;
            if (sscanf(line, " %*d %*d %31s %llu %llu %llu %llu %llu %llu %llu",
                       dev, &rios, &rmerge, &rsect, &rms, &wios, &wmerge,
                       &wsect) != 8) continue;
            if (strncmp(dev, "sd", 2) && strncmp(dev, "nvme", 4) &&
                strncmp(dev, "vd", 2)) continue;
            if (strchr(dev, 'p') && !strncmp(dev, "nvme", 4) &&
                strchr(dev + 4, 'p')) continue;   /* skip nvme partitions */
            if (!strncmp(dev, "sd", 2) && strlen(dev) > 3) continue; /* sda1... */
            rd += rsect;
            wr += wsect;
        }
        fclose(f);
    }
    f = fopen("/proc/net/dev", "r");
    if (f) {
        while (fgets(line, sizeof line, f)) {
            char ifc[32];
            unsigned long long rxb, rxp, a, b, c, d, e, g, txb;
            if (sscanf(line, " %31[^:]: %llu %llu %llu %llu %llu %llu %llu %llu %llu",
                       ifc, &rxb, &rxp, &a, &b, &c, &d, &e, &g, &txb) != 10)
                continue;
            if (!strcmp(ifc, "lo")) continue;
            rx += rxb;
            tx += txb;
        }
        fclose(f);
    }

    double t = now_mono();
    if (u->primed && *out_len + 512 < OUTBUF_CAP) {
        double dt = t - u->t_prev;
        double cps = rate_per_s(u->ctxt_prev, ctxt, dt);
        double rbps = rate_per_s(u->rd_sect_prev, rd, dt) * 512.0;
        double wbps = rate_per_s(u->wr_sect_prev, wr, dt) * 512.0;
        double rxbps = rate_per_s(u->rx_prev, rx, dt);
        double txbps = rate_per_s(u->tx_prev, tx, dt);
        if (procs_r >= 0 && cps >= 0)
            *out_len += (size_t)snprintf(out + *out_len, OUTBUF_CAP - *out_len,
                "system,server=%s load1=%.2f,load5=%.2f,load15=%.2f,"
                "procs_running=%di,ctxt_per_s=%.1f %lld\n",
                server, l1, l5, l15, procs_r, cps, (long long)ts_ns);
        if (mem_total > 0)
            *out_len += (size_t)snprintf(out + *out_len, OUTBUF_CAP - *out_len,
                "mem,server=%s used=%llui,available=%llui,cached=%llui,"
                "swap_used=%llui %lld\n",
                server, (unsigned long long)(mem_total - mem_avail),
                (unsigned long long)mem_avail, (unsigned long long)cached,
                (unsigned long long)(swap_tot - swap_free), (long long)ts_ns);
        if (rbps >= 0 && wbps >= 0)
            *out_len += (size_t)snprintf(out + *out_len, OUTBUF_CAP - *out_len,
                "disk_io,server=%s read_bps=%.1f,write_bps=%.1f %lld\n",
                server, rbps, wbps, (long long)ts_ns);
        if (rxbps >= 0 && txbps >= 0)
            *out_len += (size_t)snprintf(out + *out_len, OUTBUF_CAP - *out_len,
                "net_io,server=%s rx_bps=%.1f,tx_bps=%.1f %lld\n",
                server, rxbps, txbps, (long long)ts_ns);
    }
    u->t_prev = t;
    u->ctxt_prev = ctxt;
    u->rd_sect_prev = rd;
    u->wr_sect_prev = wr;
    u->rx_prev = rx;
    u->tx_prev = tx;
    u->primed = 1;
}

/* discover intel-rapl:N package domains (mirrors influx2.py: name == package-*) */
static int discover_rapl(int *fds, uint64_t *max_ranges) {
    glob_t g;
    int n = 0;
    if (glob("/sys/class/powercap/intel-rapl:*", GLOB_NOSORT, NULL, &g) != 0)
        return 0;
    for (size_t i = 0; i < g.gl_pathc && n < MAX_SOCKETS; i++) {
        /* skip subzones intel-rapl:N:M */
        const char *base = strrchr(g.gl_pathv[i], '/') + 1;
        if (strchr(strchr(base, ':') + 1, ':')) continue;
        char path[512], nb[64];
        snprintf(path, sizeof path, "%s/name", g.gl_pathv[i]);
        if (!read_trimmed(path, nb, sizeof nb) || strncmp(nb, "package-", 8))
            continue;
        snprintf(path, sizeof path, "%s/max_energy_range_uj", g.gl_pathv[i]);
        if (!read_trimmed(path, nb, sizeof nb)) continue;
        uint64_t mr = strtoull(nb, NULL, 10);
        snprintf(path, sizeof path, "%s/energy_uj", g.gl_pathv[i]);
        int fd = open(path, O_RDONLY);
        if (fd < 0) continue;
        fds[n] = fd;
        max_ranges[n] = mr;
        n++;
    }
    globfree(&g);
    return n;
}

/* ---- minimal keep-alive HTTP client (plain http, like the Python stack) ---- */
typedef struct {
    char host[256];
    char port[16];
    char path[512];     /* /api/v2/write?org=...&bucket=...&precision=ns */
    char auth[512];     /* Token ... */
    int fd;
} Http;

static void http_close(Http *h) {
    if (h->fd >= 0) { close(h->fd); h->fd = -1; }
}

static int http_connect(Http *h) {
    struct addrinfo hints = {0}, *res, *rp;
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(h->host, h->port, &hints, &res) != 0) return -1;
    int fd = -1;
    for (rp = res; rp; rp = rp->ai_next) {
        fd = socket(rp->ai_family, rp->ai_socktype, rp->ai_protocol);
        if (fd < 0) continue;
        struct timeval tv = { HTTP_TIMEOUT_S, 0 };
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
        if (connect(fd, rp->ai_addr, rp->ai_addrlen) == 0) break;
        close(fd);
        fd = -1;
    }
    freeaddrinfo(res);
    h->fd = fd;
    return fd < 0 ? -1 : 0;
}

static int send_all(int fd, const char *p, size_t n) {
    while (n) {
        ssize_t w = send(fd, p, n, MSG_NOSIGNAL);
        if (w <= 0) return -1;
        p += w;
        n -= (size_t)w;
    }
    return 0;
}

/* POST body; returns HTTP status or -1. One retry after reconnect. */
static int http_post(Http *h, const char *body, size_t body_len) {
    char hdr[1024];
    int hn = snprintf(hdr, sizeof hdr,
                      "POST %s HTTP/1.1\r\nHost: %s\r\nAuthorization: %s\r\n"
                      "Content-Type: text/plain\r\nContent-Length: %zu\r\n"
                      "Connection: keep-alive\r\n\r\n",
                      h->path, h->host, h->auth, body_len);
    for (int attempt = 0; attempt < 2; attempt++) {
        if (h->fd < 0 && http_connect(h) < 0) continue;
        if (send_all(h->fd, hdr, (size_t)hn) < 0 ||
            send_all(h->fd, body, body_len) < 0) {
            http_close(h);
            continue;
        }
        /* read headers (+ tiny bodies) until \r\n\r\n; drain content-length */
        char rb[2048];
        size_t got = 0;
        int status = -1;
        long clen = 0;
        char *hdr_end = NULL;
        while (got < sizeof rb - 1) {
            ssize_t r = recv(h->fd, rb + got, sizeof rb - 1 - got, 0);
            if (r <= 0) break;
            got += (size_t)r;
            rb[got] = 0;
            if ((hdr_end = strstr(rb, "\r\n\r\n"))) break;
        }
        if (!hdr_end) { http_close(h); continue; }
        sscanf(rb, "HTTP/1.%*c %d", &status);
        char *cl = strcasestr(rb, "content-length:");
        if (cl) clen = strtol(cl + 15, NULL, 10);
        long have = (long)(got - (size_t)(hdr_end + 4 - rb));
        while (have < clen) {   /* drain body so keep-alive stays in sync */
            ssize_t r = recv(h->fd, rb, sizeof rb, 0);
            if (r <= 0) { http_close(h); break; }
            have += r;
        }
        if (status < 0) { http_close(h); continue; }
        return status;
    }
    return -1;
}

/* ---------------- selfcheck ---------------- */

static void selfcheck(void) {
    /* wraparound */
    assert(energy_delta(100, 250, 1000) == 150);
    assert(energy_delta(900, 50, 1000) == 150);   /* wrapped */

    /* accumulator: zero-delta polls carry time forward, no false spike */
    Accum a = {0};
    assert(accum_add(&a, 0, 0.1) < 0);
    assert(accum_add(&a, 0, 0.1) < 0);
    double w = accum_add(&a, 30000, 0.1);   /* 0.03 J over 0.3 s = 0.1 W */
    assert(fabs(w - 0.1) < 1e-9);
    assert(a.acc_uj == 0 && a.acc_s == 0.0);

    /* near-zero-dt catch-up tick: a counter quantum landing in a sub-ms
     * window must NOT emit quantum/dt (the 1598 W false spike); it folds
     * into the next emission and the merged average stays exact */
    Accum az = {0};
    assert(accum_add(&az, 26000, 0.1) >= 0);      /* normal tick emits */
    assert(accum_add(&az, 260000, 0.0004) < 0);   /* catch-up tick suppressed */
    double wz = accum_add(&az, 26000, 0.1);       /* merged into next emit */
    assert(fabs(wz - 0.286 / 0.1004) < 1e-9);

    /* combine: total only after every socket reported once */
    Accum accs[2] = {{0}, {0}};
    double lastw[2] = {-1, -1};
    uint64_t d1[2] = {100000, 0};
    assert(combine_watts(d1, 0.1, accs, lastw, 2) < 0);   /* socket 1 silent */
    uint64_t d2[2] = {100000, 200000};
    double tot = combine_watts(d2, 0.1, accs, lastw, 2);
    assert(fabs(tot - (1.0 + 1.0)) < 1e-9);   /* 0.1J/0.1s + 0.2J/0.2s */

    /* edge score parity with hf_edge_scorer.selfcheck() */
    Samples s = {0};
    for (int i = 0; i < NSAMP; i++) samples_push(&s, 1);
    assert(edge_score(&s) == 0.0);
    samples_push(&s, 40);
    assert(edge_score(&s) == 39.0);
    Samples s2 = {0};
    for (int i = 0; i < NSAMP - 1; i++) samples_push(&s2, 1);
    assert(edge_score(&s2) < 0);   /* not warm yet -> None */

    /* threshold: warmup floor, then quantile with numpy-linear interp */
    static double sv[SCORE_WINDOW], sc[SCORE_WINDOW];
    Scores sco = { sv, 0, 0, sc };
    scores_push(&sco, 19.0);
    assert(live_threshold(&sco) == MIN_EDGE_DELTA);
    for (int i = 0; i < MIN_RANK_SCORES; i++)
        scores_push(&sco, 100.0 * i / (MIN_RANK_SCORES - 1));   /* linspace 0..100 */
    assert(live_threshold(&sco) > MIN_EDGE_DELTA);
    double srt[5] = {0, 10, 20, 30, 40};
    assert(fabs(quantile_linear(srt, 5, 0.995) - 39.8) < 1e-9);

    /* ring eviction at capacity */
    Scores sco2 = { sv, 0, 0, sc };
    for (int i = 0; i < SCORE_WINDOW + 5; i++) scores_push(&sco2, (double)i);
    assert(sco2.n == SCORE_WINDOW);

    /* line protocol formats (types must match the Python writers) */
    char lb[512];
    lp_power(lb, sizeof lb, "mycroft", 123.456789, 1783620866123456789LL);
    assert(!strcmp(lb, "cpu_power,server=mycroft watts=123.457 1783620866123456789\n"));
    lp_runq(lb, sizeof lb, "mycroft", 12, 1LL);
    assert(!strcmp(lb, "run_queue,server=mycroft nr_running=12i 1\n"));
    lp_edge(lb, sizeof lb, "mycroft", 12, 39.0, 20.0, 1, 600, 2LL);
    assert(!strcmp(lb, EDGE_MEASUREMENT ",server=mycroft nr_running=12i,"
                       "edge_score=39.000,edge_threshold=20.000,spike_risk=1i,"
                       "rank_buffer_n=600i 2\n"));

    /* per-core stats: /proc/stat math + line protocol parity with influx2.py */
    int cpu;
    uint64_t bz, tt;
    assert(parse_cpu_line("cpu7 100 0 100 700 100 0 0 0", &cpu, &bz, &tt));
    assert(cpu == 7 && tt == 1000 && bz == 200);
    assert(!parse_cpu_line("intr 12345", &cpu, &bz, &tt));
    assert(cpu_percent(200, 1000) == 20.0);
    assert(cpu_percent(0, 0) < 0);          /* no delta yet -> no point */
    assert(cpu_percent(2000, 1000) == 100.0);   /* clamped */
    lp_usage(lb, sizeof lb, "mycroft", 7, 42.5, 3LL);
    assert(!strcmp(lb, "cpu_usage,server=mycroft,core=7 percent=42.500 3\n"));
    lp_freq(lb, sizeof lb, "mycroft", 0, 2900.0, 4LL);
    assert(!strcmp(lb, "cpu_freq,server=mycroft,core=0 mhz=2900.000 4\n"));
    lp_temp(lb, sizeof lb, "mycroft", "core=3", 55.0, 5LL);
    assert(!strcmp(lb, "cpu_temp,server=mycroft,core=3 celsius=55.0 5\n"));
    assert(rate_per_s(100, 250, 1.5) == 100.0);
    assert(rate_per_s(250, 100, 1.0) < 0);   /* counter reset -> skip point */
    assert(rate_per_s(0, 0, 0.0) < 0);

    printf("selfcheck OK: wraparound, accumulator, combine, edge score, "
           "threshold quantile, ring, line protocol, per-core stats\n");
}

/* ---------------- main loop ---------------- */

int main(int argc, char **argv) {
    const char *server = NULL, *url = DEFAULT_URL;
    int dry_run = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--selfcheck")) { selfcheck(); return 0; }
        else if (!strcmp(argv[i], "--dry-run")) dry_run = 1;
        else if (!strcmp(argv[i], "--server") && i + 1 < argc) server = argv[++i];
        else if (!strcmp(argv[i], "--url") && i + 1 < argc) url = argv[++i];
        else die("usage: %s [--selfcheck] [--dry-run] [--server NAME] [--url http://host:port]",
                 argv[0]);
    }

    char hostbuf[256];
    if (!server) {
        gethostname(hostbuf, sizeof hostbuf);
        hostbuf[sizeof hostbuf - 1] = 0;
        char *dot = strchr(hostbuf, '.');
        if (dot) *dot = 0;
        server = hostbuf;
    }

    Http http = { .fd = -1 };
    if (!dry_run) {
        const char *p = url;
        if (!strncmp(p, "http://", 7)) p += 7;
        else if (strstr(p, "://")) die("only plain http:// URLs are supported");
        const char *colon = strchr(p, ':');
        if (!colon) die("URL must be http://host:port");
        snprintf(http.host, sizeof http.host, "%.*s", (int)(colon - p), p);
        snprintf(http.port, sizeof http.port, "%s", colon + 1);

        const char *home = getenv("HOME");
        if (!home) {
            struct passwd *pw = getpwuid(getuid());
            home = pw ? pw->pw_dir : NULL;
        }
        if (!home) die("cannot resolve HOME for ~/.secrets");
        char path[512], org[128], token[256];
        snprintf(path, sizeof path, "%s/.secrets/influx_org.txt", home);
        if (!read_trimmed(path, org, sizeof org)) die("cannot read %s", path);
        snprintf(path, sizeof path, "%s/.secrets/influx_token.txt", home);
        if (!read_trimmed(path, token, sizeof token)) die("cannot read %s", path);
        snprintf(http.path, sizeof http.path,
                 "/api/v2/write?org=%s&bucket=%s&precision=ns", org, BUCKET);
        snprintf(http.auth, sizeof http.auth, "Token %s", token);
    }

    int fds[MAX_SOCKETS];
    uint64_t max_ranges[MAX_SOCKETS];
    int nsock = discover_rapl(fds, max_ranges);
    if (nsock == 0) {
        if (!dry_run)
            die("no readable intel-rapl package domains (need root for energy_uj)");
        fprintf(stderr, "dry-run: no readable RAPL domains, power stream disabled\n");
    }
    fprintf(stderr, "rapl_hf_sampler: server=%s sockets=%d sample=%.0fHz "
                    "edge_write=%.1fHz flush=%.0fs %s\n",
            server, nsock, SAMPLE_HZ, EDGE_WRITE_HZ, FLUSH_S,
            dry_run ? "(dry-run)" : "");

    static double score_vals[SCORE_WINDOW], score_scratch[SCORE_WINDOW];
    Scores scores = { score_vals, 0, 0, score_scratch };
    Samples samples = {0};
    Accum accs[MAX_SOCKETS] = {{0}};
    double last_watts[MAX_SOCKETS];
    uint64_t last_energy[MAX_SOCKETS];
    for (int i = 0; i < nsock; i++) {
        last_watts[i] = -1.0;
        last_energy[i] = read_u64_fd(fds[i]);
    }

    static CpuStats cs;
    static UpStats us;
    discover_temps(&cs);
    fprintf(stderr, "rapl_hf_sampler: %d temp sensors, per-core stats @%gs\n",
            cs.ntemp, CPUSTATS_S);

    static char out[OUTBUF_CAP];
    size_t out_len = 0;
    double last_mono = now_mono();
    double next_stats = last_mono;   /* first tick = baseline (no usage points) */
    double next_tick = last_mono + 1.0 / SAMPLE_HZ;
    double last_edge_write = -1e9, last_flag = -1e9, last_thr = -1e9;
    double last_flush = last_mono;
    double thr = MIN_EDGE_DELTA;
    long dropped = 0;

    for (;;) {
        double now = now_mono();
        if (next_tick > now) {
            struct timespec ts = { (time_t)(next_tick - now),
                                   (long)(fmod(next_tick - now, 1.0) * 1e9) };
            nanosleep(&ts, NULL);
        } else if (now - next_tick > 1.0) {
            next_tick = now;   /* fell far behind; resync instead of bursting */
        }
        double t = now_mono();
        int64_t ts_ns = now_ns();
        int flush_now = 0;

        /* power: per-socket wraparound deltas -> accumulators -> summed watts */
        if (nsock > 0) {
            uint64_t deltas[MAX_SOCKETS];
            int ok = 1;
            for (int i = 0; i < nsock; i++) {
                uint64_t e = read_u64_fd(fds[i]);
                if (e == UINT64_MAX) { ok = 0; break; }
                deltas[i] = energy_delta(last_energy[i], e, max_ranges[i]);
                last_energy[i] = e;
            }
            if (ok) {
                double watts = combine_watts(deltas, t - last_mono, accs,
                                             last_watts, nsock);
                if (watts >= 0 && out_len + 128 < OUTBUF_CAP)
                    out_len += (size_t)lp_power(out + out_len,
                                                OUTBUF_CAP - out_len,
                                                server, watts, ts_ns);
                if (watts >= 0 && out_len + 128 < OUTBUF_CAP) {
                    int nr_ = read_nr_running();
                    if (nr_ >= 0)
                        out_len += (size_t)lp_runq(out + out_len,
                                                   OUTBUF_CAP - out_len,
                                                   server, nr_, ts_ns);
                }
            }
        }
        last_mono = t;

        /* edge score on the run queue (independent read: also works w/o RAPL) */
        int nr = read_nr_running();
        if (nr >= 0) {
            samples_push(&samples, nr);
            double score = edge_score(&samples);
            if (score >= 0) {
                if (t - last_thr >= THR_REFRESH_S) {
                    thr = live_threshold(&scores);
                    last_thr = t;
                }
                int rank_n = scores.n;
                int risk = score > thr && (t - last_flag) >= REFRACTORY_S;
                scores_push(&scores, score);   /* after decision, like Python */
                if (risk) last_flag = t;
                if (risk || t - last_edge_write >= 1.0 / EDGE_WRITE_HZ) {
                    if (out_len + 256 < OUTBUF_CAP)
                        out_len += (size_t)lp_edge(out + out_len,
                                                   OUTBUF_CAP - out_len, server,
                                                   nr, score, thr, risk, rank_n,
                                                   ts_ns);
                    last_edge_write = t;
                }
                if (risk) flush_now = 1;   /* flags must not sit in the buffer */
            }
        }

        /* 1 Hz per-core usage/freq/temp (influx2.py psutil-half parity) */
        if (t >= next_stats) {
            emit_cpu_stats(&cs, server, ts_ns, out, &out_len);
            emit_upstream(&us, server, ts_ns, out, &out_len);
            next_stats += CPUSTATS_S;
            if (next_stats < t) next_stats = t + CPUSTATS_S;
        }

        /* batched flush: one POST per FLUSH_S (or now, on a risk edge) */
        if (out_len && (flush_now || t - last_flush >= FLUSH_S ||
                        out_len > OUTBUF_CAP - 4096)) {
            if (dry_run) {
                fwrite(out, 1, out_len, stdout);
                fflush(stdout);
                out_len = 0;
            } else {
                int status = http_post(&http, out, out_len);
                if (status >= 200 && status < 300) {
                    out_len = 0;
                } else if (out_len > OUTBUF_CAP - 4096) {
                    dropped++;
                    out_len = 0;   /* outage + full buffer: drop batch, keep sampling */
                    if (dropped == 1 || dropped % 100 == 0)
                        fprintf(stderr, "write failed (status %d); dropped %ld batches so far\n",
                                status, dropped);
                }
            }
            last_flush = t;
        }

        next_tick += 1.0 / SAMPLE_HZ;
    }
}

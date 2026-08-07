/* kf_predict.c — ClassicalKF (kalmannet.py) ported to C for the sampler fold-in.
 *
 * 2-state local level+trend KF, dt=1 s, with the 2026-07-09 adaptive noise
 * model: R re-estimated from gated innovation variance (Mehra), q riding a
 * NIS consistency check, innov^2 winsorized at GATE_NIS*S so a real power
 * step can't blow up q (the live runaway bug). kf_step() is the unit the C
 * scorer embeds; this file wraps it in a selfcheck + golden-trace verifier.
 *
 * Build:      cc -O2 -Wall -o kf_predict kf_predict.c -lm
 * Selfcheck:  ./kf_predict --selfcheck
 * Parity:     .venv/bin/python kf_export.py --paritycheck   (drives --verify)
 */
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* constants mirror kalmannet.py exactly */
#define ADAPT_ALPHA 0.02
#define KF_R_MIN 1.0
#define KF_R_MAX 1e4
#define KF_Q_MIN 1e-4
#define KF_Q_MAX 1e3
#define GATE_NIS 9.0

typedef struct {
    double q, R;
    double m0, m1;                  /* state: level, slope */
    double P00, P01, P10, P11;
    double c_innov, nis;
    int adapt;
} KF;

static void kf_init(KF *k, double q, double r, double z0, int adapt) {
    k->q = q; k->R = r;
    k->m0 = z0; k->m1 = 0.0;
    k->P00 = 1e3; k->P01 = 0.0; k->P10 = 0.0; k->P11 = 1e3;
    k->c_innov = r; k->nis = 1.0;
    k->adapt = adapt;
}

/* One filter step. Returns pred_next (one-step-ahead measurement prediction);
 * filtered level/slope and innovation via out-pointers. Arithmetic ordered to
 * match numpy's F@P@F.T etc. so parity vs Python is bit-tight. */
static double kf_step(KF *k, double z, double *level, double *slope,
                      double *innov_out) {
    /* Q = q * QC, QC = [[1/3,1/2],[1/2,1]] (dt=1 WNA discretisation) */
    double Q00 = k->q * (1.0 / 3.0), Q01 = k->q * 0.5, Q11 = k->q;
    /* predict: m_pred = F m; P_pred = F P F^T + Q, F = [[1,1],[0,1]] */
    double mp0 = k->m0 + k->m1, mp1 = k->m1;
    double Pp00 = (k->P00 + k->P10) + (k->P01 + k->P11) + Q00;
    double Pp01 = k->P01 + k->P11 + Q01;
    double Pp10 = k->P10 + k->P11 + Q01;
    double Pp11 = k->P11 + Q11;
    /* update, H = [1,0] */
    double innov = z - mp0;
    double HPHt = Pp00;
    double S = HPHt + k->R;
    double K0 = Pp00 / S, K1 = Pp10 / S;
    k->m0 = mp0 + K0 * innov;
    k->m1 = mp1 + K1 * innov;
    /* P = (I - outer(K,H)) P_pred = [[1-K0,0],[-K1,1]] P_pred */
    k->P00 = (1.0 - K0) * Pp00;
    k->P01 = (1.0 - K0) * Pp01;
    k->P10 = Pp10 - K1 * Pp00;
    k->P11 = Pp11 - K1 * Pp01;
    if (k->adapt) {
        double i2 = innov * innov;
        if (i2 > GATE_NIS * S) i2 = GATE_NIS * S;   /* the winsorize gate */
        k->c_innov = (1.0 - ADAPT_ALPHA) * k->c_innov + ADAPT_ALPHA * i2;
        double R = k->c_innov - HPHt;
        k->R = R < KF_R_MIN ? KF_R_MIN : (R > KF_R_MAX ? KF_R_MAX : R);
        k->nis = (1.0 - ADAPT_ALPHA) * k->nis + ADAPT_ALPHA * (i2 / S);
        double q = k->q * pow(k->nis, ADAPT_ALPHA);
        k->q = q < KF_Q_MIN ? KF_Q_MIN : (q > KF_Q_MAX ? KF_Q_MAX : q);
    }
    *level = k->m0;
    *slope = k->m1;
    *innov_out = innov;
    return k->m0 + k->m1;           /* (H F m_new)[0] */
}

/* --verify GOLDEN: header `kf_golden v1 q Q r R adapt A n N`, then N rows of
 * z,pred_next,level,slope,innov,q,R written by kf_export.py at full precision */
static int verify(const char *path) {
    FILE *fp = fopen(path, "r");
    if (!fp) { perror(path); return 1; }
    double q, r;
    int adapt, n;
    if (fscanf(fp, "kf_golden v1 q %lf r %lf adapt %d n %d",
               &q, &r, &adapt, &n) != 4 || n <= 0) {
        fprintf(stderr, "%s: malformed golden header\n", path);
        fclose(fp);
        return 1;
    }
    KF k;
    double maxerr = 0.0;
    for (int t = 0; t < n; t++) {
        double z, w[6], g[6];       /* want/got: pred,level,slope,innov,q,R */
        if (fscanf(fp, " %lf ,%lf ,%lf ,%lf ,%lf ,%lf ,%lf", &z,
                   &w[0], &w[1], &w[2], &w[3], &w[4], &w[5]) != 7) {
            fprintf(stderr, "%s: truncated at row %d\n", path, t);
            fclose(fp);
            return 1;
        }
        if (t == 0) kf_init(&k, q, r, z, adapt);   /* python seeds z0=z[0] */
        g[0] = kf_step(&k, z, &g[1], &g[2], &g[3]);
        g[4] = k.q; g[5] = k.R;
        for (int i = 0; i < 6; i++) {
            double err = fabs(g[i] - w[i]);
            double tol = 1e-9 * fmax(1.0, fabs(w[i]));
            if (err > maxerr) maxerr = err;
            if (err > tol) {
                printf("PARITY FAIL row %d field %d: got %.17g want %.17g\n",
                       t, i, g[i], w[i]);
                fclose(fp);
                return 1;
            }
        }
    }
    fclose(fp);
    printf("PARITY OK: %d steps (q=%g r=%g adapt=%d), max |err| %.3g\n",
           n, q, r, adapt, maxerr);
    return 0;
}

static void selfcheck(void) {
    KF k;
    double lvl, slp, in;
    /* (a) constant signal: level converges to z, slope/innov to ~0 */
    kf_init(&k, 0.01, 25.0, 5.0, 0);
    for (int t = 0; t < 500; t++) kf_step(&k, 5.0, &lvl, &slp, &in);
    assert(fabs(lvl - 5.0) < 1e-6 && fabs(slp) < 1e-6 && fabs(in) < 1e-6);
    /* (b) linear ramp: trend state locks on, innovations die out */
    kf_init(&k, 0.01, 25.0, 0.0, 0);
    for (int t = 1; t <= 500; t++) kf_step(&k, (double)t, &lvl, &slp, &in);
    assert(fabs(slp - 1.0) < 1e-3 && fabs(in) < 1e-3);
    /* (c) gate regression (kalmannet selfcheck (e) analogue): calm stream then
     * a +1000 W single-sample outlier must nudge q boundedly, not kick it */
    kf_init(&k, 0.1, 100.0, 200.0, 1);
    for (int t = 0; t < 300; t++) kf_step(&k, 200.0, &lvl, &slp, &in);
    double q_before = k.q;
    kf_step(&k, 1200.0, &lvl, &slp, &in);
    assert(k.q < q_before * 2.0);   /* ungated, innov^2/S alone is ~1e4 */
    assert(k.R >= KF_R_MIN && k.R <= KF_R_MAX);
    printf("selfcheck OK: convergence, ramp tracking, innovation gate\n");
}

int main(int argc, char **argv) {
    if (argc >= 2 && !strcmp(argv[1], "--selfcheck")) { selfcheck(); return 0; }
    if (argc == 3 && !strcmp(argv[1], "--verify")) return verify(argv[2]);
    fprintf(stderr, "usage: %s --selfcheck | --verify golden.csv\n", argv[0]);
    return 2;
}

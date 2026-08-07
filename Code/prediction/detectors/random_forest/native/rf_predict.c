/* rf_predict.c — RandomForest inference in C for the spike model.
 *
 * Loads the flat forest written by rf_export.py and evaluates feature
 * vectors: proba = mean over trees of the reached leaf's class-1 fraction
 * (exact sklearn predict_proba semantics for a classification RF).
 *
 * This is the inference core for the C prediction daemon; feature
 * computation from live telemetry is the next phase (until then the Python
 * scorer stays the live writer).
 *
 * Build:      cc -O2 -Wall -o rf_predict rf_predict.c -lm
 * Selfcheck:  ./rf_predict --selfcheck
 * Parity:     .venv/bin/python rf_export.py --paritycheck   (drives --verify)
 */
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {                 /* one flattened tree, arrays of node_count */
    int nnodes;
    int *feature, *left, *right; /* feature < 0 -> leaf */
    double *thr, *leaf_p1;
} Tree;

typedef struct {
    char version[64];
    double threshold;
    int ncols, ntrees;
    char **cols;
    Tree *trees;
} Forest;

static double tree_predict(const Tree *t, const double *x) {
    int n = 0;
    while (t->feature[n] >= 0)
        n = (x[t->feature[n]] <= t->thr[n]) ? t->left[n] : t->right[n];
    return t->leaf_p1[n];
}

static double forest_predict(const Forest *f, const double *x) {
    double s = 0.0;
    for (int i = 0; i < f->ntrees; i++) s += tree_predict(&f->trees[i], x);
    return s / f->ntrees;
}

static Forest *forest_load(const char *path) {
    FILE *fp = fopen(path, "r");
    if (!fp) { perror(path); return NULL; }
    Forest *f = calloc(1, sizeof *f);
    char word[64];
    if (fscanf(fp, "rf_forest %63s", word) != 1 || strcmp(word, "v1")) goto bad;
    if (fscanf(fp, " version %63s threshold %lf ncols %d",
               f->version, &f->threshold, &f->ncols) != 3) goto bad;
    if (fscanf(fp, " %63s", word) != 1 || strcmp(word, "cols")) goto bad;
    f->cols = calloc((size_t)f->ncols, sizeof *f->cols);
    for (int i = 0; i < f->ncols; i++) {
        if (fscanf(fp, " %63s", word) != 1) goto bad;
        f->cols[i] = strdup(word);
    }
    if (fscanf(fp, " ntrees %d", &f->ntrees) != 1 || f->ntrees <= 0) goto bad;
    f->trees = calloc((size_t)f->ntrees, sizeof *f->trees);
    for (int t = 0; t < f->ntrees; t++) {
        Tree *tr = &f->trees[t];
        if (fscanf(fp, " tree %d", &tr->nnodes) != 1 || tr->nnodes <= 0) goto bad;
        tr->feature = malloc((size_t)tr->nnodes * sizeof *tr->feature);
        tr->left    = malloc((size_t)tr->nnodes * sizeof *tr->left);
        tr->right   = malloc((size_t)tr->nnodes * sizeof *tr->right);
        tr->thr     = malloc((size_t)tr->nnodes * sizeof *tr->thr);
        tr->leaf_p1 = malloc((size_t)tr->nnodes * sizeof *tr->leaf_p1);
        for (int n = 0; n < tr->nnodes; n++)
            if (fscanf(fp, " %d %lf %d %d %lf", &tr->feature[n], &tr->thr[n],
                       &tr->left[n], &tr->right[n], &tr->leaf_p1[n]) != 5)
                goto bad;
    }
    fclose(fp);
    return f;
bad:
    fprintf(stderr, "%s: malformed forest file\n", path);
    fclose(fp);
    return NULL;
}

/* --verify FOREST CSV: rows of ncols features + expected proba */
static int verify(const char *fpath, const char *csv) {
    Forest *f = forest_load(fpath);
    if (!f) return 1;
    FILE *fp = fopen(csv, "r");
    if (!fp) { perror(csv); return 1; }
    double *x = malloc((size_t)f->ncols * sizeof *x);
    int rows = 0;
    double maxerr = 0.0;
    while (1) {
        int i;
        for (i = 0; i < f->ncols; i++)
            if (fscanf(fp, " %lf ,", &x[i]) != 1) goto done;
        double want;
        if (fscanf(fp, " %lf", &want) != 1) goto done;
        double got = forest_predict(f, x);
        double err = fabs(got - want);
        if (err > maxerr) maxerr = err;
        if (err > 1e-9) {
            printf("PARITY FAIL row %d: got %.12f want %.12f\n", rows, got, want);
            return 1;
        }
        rows++;
    }
done:
    printf("PARITY OK: %d rows, %d trees, %d cols, max |err| %.3g "
           "(model %s, threshold %g)\n",
           rows, f->ntrees, f->ncols, maxerr, f->version, f->threshold);
    return rows > 0 ? 0 : 1;
}

static void selfcheck(void) {
    /* hand-built stump: x0 <= 0.5 -> p 0.2 else p 0.8 */
    int feat[3] = {0, -1, -1}, l[3] = {1, -1, -1}, r[3] = {2, -1, -1};
    double thr[3] = {0.5, 0, 0}, p1[3] = {0, 0.2, 0.8};
    Tree t = {3, feat, l, r, thr, p1};
    double lo = 0.0, hi = 1.0;
    assert(tree_predict(&t, &lo) == 0.2 && tree_predict(&t, &hi) == 0.8);
    Forest f = {"self", 0.5, 1, 2, NULL, (Tree[]){t, t}};
    assert(forest_predict(&f, &lo) == 0.2);   /* mean of identical trees */
    printf("selfcheck OK: tree walk, forest mean\n");
}

int main(int argc, char **argv) {
    if (argc >= 2 && !strcmp(argv[1], "--selfcheck")) { selfcheck(); return 0; }
    if (argc == 4 && !strcmp(argv[1], "--verify"))
        return verify(argv[2], argv[3]);
    fprintf(stderr, "usage: %s --selfcheck | --verify forest.txt test.csv\n",
            argv[0]);
    return 2;
}

#!/usr/bin/env bash
# Score results/<run>/ dirs with grid_metrics.py, looking up --gflops from
# data/sweep_meta.csv (mapping _worst / _sfNNN variants back to their base
# trace). Writes metrics.json into each dir.
#
#   scripts/score_all.sh                                  # every results dir
#   scripts/score_all.sh rapl_hpl_noctl rapl_hpl_noctl_worst   # only these
#   WARMUP_S=45 scripts/score_all.sh                      # drop the t=0 cold start
#
# Run scripts/compare.py-side by hand afterwards:  python3 analysis/compare.py
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - "$@" << 'PY'
import subprocess, os, csv, re, sys

meta = {}
try:
    with open('data/sweep_meta.csv') as f:
        meta = {r['run_name']: r['gflops'] for r in csv.DictReader(f)}
except FileNotFoundError:
    pass

dirs = sys.argv[1:] or sorted(os.listdir('results'))
for d in dirs:
    p = os.path.join('results', d)
    if not os.path.isdir(p):
        print(f'skip {d} (not a results dir)'); continue
    # _sfNNN and _worst are aggregation variants of a base trace; strip to base
    # for the gflops lookup. The bare-name fallback matches diverse runs.
    base = (re.match(r'(.*)_sf\d{3}(?:_s\d+)?$', d) or re.match(r'(.*)_worst$', d)
            or re.match(r'(.*)$', d)).group(1)
    g = meta.get(base)
    cmd = ['python3', '../../analysis/grid_metrics.py']
    w = os.environ.get('WARMUP_S')
    if w:
        cmd += ['--warmup-s', w]
    if g not in (None, '', 'NA'):
        cmd += ['--gflops', g]
    subprocess.run(cmd, cwd=p, check=True)
    print(f'scored {d}' + (f' (gflops={g})' if g else ' (no gflops)'))
PY

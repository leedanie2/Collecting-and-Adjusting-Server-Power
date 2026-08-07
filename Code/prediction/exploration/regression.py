#!/usr/bin/env python3
"""Multivariate linear regression: power_watts ~ freq_mhz + temp_celsius + usage_percent.

Plain OLS via numpy (no new ML dependency) since this is 3 predictors on a
few thousand rows -- nothing here needs sklearn/statsmodels.
"""

import argparse
from itertools import combinations_with_replacement

import numpy as np

from core import telemetry as common

PREDICTORS = ["freq_mhz", "temp_celsius", "usage_percent"]
TARGET = "power_watts"


def fit_ols(df):
    data = df[PREDICTORS + [TARGET]].dropna()
    X = np.column_stack([np.ones(len(data)), data[PREDICTORS].to_numpy()])
    y = data[TARGET].to_numpy()

    coefs, *_ = np.linalg.lstsq(X, y, rcond=None)
    fitted = X @ coefs
    resid = y - fitted

    ss_res = (resid ** 2).sum()
    ss_tot = ((y - y.mean()) ** 2).sum()
    r2 = 1 - ss_res / ss_tot
    n, p = X.shape
    adj_r2 = 1 - (1 - r2) * (n - 1) / (n - p)
    rmse = np.sqrt(ss_res / n)

    # Standardized coefficients (z-scored), for relative importance --
    # freq/temp/usage have wildly different scales (mhz vs celsius vs %).
    z = (data[PREDICTORS] - data[PREDICTORS].mean()) / data[PREDICTORS].std()
    zy = (data[TARGET] - data[TARGET].mean()) / data[TARGET].std()
    Xz = np.column_stack([np.ones(len(z)), z.to_numpy()])
    std_coefs, *_ = np.linalg.lstsq(Xz, zy.to_numpy(), rcond=None)

    return coefs, std_coefs, r2, adj_r2, rmse, n


def fit_poly(df, degree, predictor="usage_percent"):
    data = df[[predictor, TARGET]].dropna()
    x = data[predictor].to_numpy()
    y = data[TARGET].to_numpy()

    coefs = np.polyfit(x, y, degree)  # highest power first
    fitted = np.polyval(coefs, x)
    resid = y - fitted

    ss_res = (resid ** 2).sum()
    ss_tot = ((y - y.mean()) ** 2).sum()
    r2 = 1 - ss_res / ss_tot
    n = len(x)
    p = degree + 1
    adj_r2 = 1 - (1 - r2) * (n - 1) / (n - p)
    rmse = np.sqrt(ss_res / n)

    return coefs, r2, adj_r2, rmse, n


def run_poly(df, degrees, predictor="usage_percent"):
    print(f"power_watts ~ poly({predictor}, degree)\n")
    print(f"{'degree':>6}  {'R2':>8}  {'adj_R2':>8}  {'RMSE_W':>8}")
    for d in degrees:
        coefs, r2, adj_r2, rmse, n = fit_poly(df, d, predictor)
        print(f"{d:>6}  {r2:>8.4f}  {adj_r2:>8.4f}  {rmse:>8.2f}")
    print(f"\n(n={n})")
    print("\nHighest fitted degree's coefficients (highest power first):")
    print("  " + "  ".join(f"{c:+.4e}" for c in coefs))
    print(
        "\nWatch adj_R2, not raw R2 -- raw R2 never decreases as degree goes up, "
        "even when the extra terms are just fitting noise. If adj_R2 stops "
        "improving (or drops) past some degree, higher-order terms aren't earning "
        "their keep."
    )


def poly_design_matrix(data, predictors, degree):
    """Full polynomial expansion (all monomials up to total degree, incl.
    interactions) -- same thing sklearn's PolynomialFeatures(degree) builds,
    done by hand with itertools so we don't add a new dependency for it."""
    X_raw = data[predictors].to_numpy()
    n = len(data)
    cols = [np.ones(n)]
    names = ["1"]
    for d in range(1, degree + 1):
        for combo in combinations_with_replacement(range(len(predictors)), d):
            col = np.ones(n)
            for idx in combo:
                col = col * X_raw[:, idx]
            cols.append(col)
            names.append("*".join(predictors[i] for i in combo))
    return np.column_stack(cols), names


def fit_mv_poly(df, degree):
    data = df[PREDICTORS + [TARGET]].dropna()
    X, names = poly_design_matrix(data, PREDICTORS, degree)
    y = data[TARGET].to_numpy()

    coefs, *_ = np.linalg.lstsq(X, y, rcond=None)
    fitted = X @ coefs
    resid = y - fitted

    ss_res = (resid ** 2).sum()
    ss_tot = ((y - y.mean()) ** 2).sum()
    r2 = 1 - ss_res / ss_tot
    n_rows, p = X.shape
    adj_r2 = 1 - (1 - r2) * (n_rows - 1) / (n_rows - p)
    rmse = np.sqrt(ss_res / n_rows)

    return coefs, names, r2, adj_r2, rmse, n_rows


def run_mv_poly(df, degrees):
    print(f"power_watts ~ poly({', '.join(PREDICTORS)}; degree, incl. interactions)\n")
    print(f"{'degree':>6}  {'#terms':>7}  {'R2':>8}  {'adj_R2':>8}  {'RMSE_W':>8}")
    last = None
    for d in degrees:
        coefs, names, r2, adj_r2, rmse, n_rows = fit_mv_poly(df, d)
        print(f"{d:>6}  {len(names):>7}  {r2:>8.4f}  {adj_r2:>8.4f}  {rmse:>8.2f}")
        last = (coefs, names)
    print(f"\n(n={n_rows})")

    coefs, names = last
    print(f"\nHighest fitted degree's coefficients:")
    for name, c in zip(names, coefs):
        print(f"  {name:30s} {c:+.4e}")
    print(
        "\nWatch adj_R2 and #terms together -- with 3 predictors, #terms grows fast "
        "with degree (interactions included), so a higher R2 at a much larger #terms "
        "may just mean more room to fit noise, not a better model."
    )


def main():
    p = argparse.ArgumentParser(description="Regression: power_watts ~ predictors.")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--poly-degrees", default=None,
                    help="Comma-separated degrees for a univariate usage_percent->power_watts "
                         "polynomial fit instead of the default multivariate linear fit, e.g. 1,2,3,4")
    p.add_argument("--poly-predictor", default="usage_percent",
                    help="Predictor column for --poly-degrees (default: usage_percent)")
    p.add_argument("--mv-poly-degrees", default=None,
                    help="Comma-separated degrees for a full multivariate polynomial fit "
                         "(freq_mhz, temp_celsius, usage_percent, with interactions), e.g. 1,2,3")
    args = p.parse_args()

    df = common.load_telemetry(args.start, args.end)

    if args.poly_degrees:
        degrees = [int(d) for d in args.poly_degrees.split(",")]
        run_poly(df, degrees, args.poly_predictor)
        return

    if args.mv_poly_degrees:
        degrees = [int(d) for d in args.mv_poly_degrees.split(",")]
        run_mv_poly(df, degrees)
        return

    coefs, std_coefs, r2, adj_r2, rmse, n = fit_ols(df)

    print(f"power_watts ~ {' + '.join(PREDICTORS)}   (n={n})")
    print(f"\nintercept: {coefs[0]:.3f}")
    for name, c in zip(PREDICTORS, coefs[1:]):
        print(f"  {name:14s} coef={c:+.4f} watts per unit")

    print("\nStandardized coefficients (relative importance, unit-free):")
    for name, c in zip(PREDICTORS, std_coefs[1:]):
        print(f"  {name:14s} {c:+.3f}")

    print(f"\nR2={r2:.4f}  adj_R2={adj_r2:.4f}  RMSE={rmse:.2f} W")


if __name__ == "__main__":
    main()

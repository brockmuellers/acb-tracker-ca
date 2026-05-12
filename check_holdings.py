"""Compare a holdings CSV against an expected (golden) file.

Exits 0 if identical, 1 if differences are found.

Usage:
    python3 check_holdings.py <expected.csv> <actual.csv>
"""

import argparse
import csv
import sys
from decimal import Decimal, InvalidOperation


def load_holdings(path):
    rows = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            key = (row["account_number"], row["ticker"])
            if key in rows:
                sys.exit(f"Duplicate row in {path}: account={row['account_number']} ticker={row['ticker']}")
            rows[key] = row
    return rows


def _diff_field(col, exp_val, act_val):
    if exp_val == act_val:
        return None
    try:
        delta = Decimal(act_val) - Decimal(exp_val)
        sign = "+" if delta > 0 else ""
        return f"{col}: {exp_val} → {act_val} ({sign}{delta})"
    except InvalidOperation:
        return f"{col}: {exp_val!r} → {act_val!r}"


def diff_holdings(expected, actual):
    """Compare two holdings dicts (keyed by (account_number, ticker)).

    Returns a list of human-readable diff lines, empty if identical.
    """
    diffs = []

    for key in sorted(expected):
        acct, ticker = key
        exp_row = expected[key]
        if key not in actual:
            diffs.append(
                f"  MISSING   {acct:<22} {ticker:<12} "
                f"qty={exp_row['quantity']}  acb={exp_row['acb_cad']}"
            )
        else:
            act_row = actual[key]
            field_diffs = [
                d for col in ("quantity", "acb_cad")
                if (d := _diff_field(col, exp_row[col], act_row[col])) is not None
            ]
            if field_diffs:
                diffs.append(
                    f"  CHANGED   {acct:<22} {ticker:<12} " + ",  ".join(field_diffs)
                )

    for key in sorted(actual):
        if key not in expected:
            acct, ticker = key
            act_row = actual[key]
            diffs.append(
                f"  EXTRA     {acct:<22} {ticker:<12} "
                f"qty={act_row['quantity']}  acb={act_row['acb_cad']}"
            )

    return diffs


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("expected", help="expected (golden) holdings CSV")
    parser.add_argument("actual", help="actual holdings CSV to check")
    args = parser.parse_args()

    diffs = diff_holdings(load_holdings(args.expected), load_holdings(args.actual))

    if diffs:
        print(f"Holdings differ ({len(diffs)} difference(s)):")
        for line in diffs:
            print(line)
        sys.exit(1)

    print("Holdings match.")


if __name__ == "__main__":
    main()

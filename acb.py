"""ACB (Adjusted Cost Base) calculator for Canadian CRA tax reporting.

Reads one or more CSVs of transactions and emits a CSV with a running ACB
column per ticker, in chronological order.

Input columns (header required, order flexible):
    date      ISO 8601 (YYYY-MM-DD)
    ticker    string (normalized to upper-case)
    type      START, BUY, or SELL (case-insensitive)
    quantity  decimal, positive
    price     per-share price, decimal, positive (CAD)

A START row declares an opening balance for a ticker — `quantity` is the
shares already held and `price` is their per-share ACB. A ticker may have
at most one START, and it must come before any BUY/SELL for that ticker
chronologically (the run errors clearly otherwise). Mathematically a
START is identical to a BUY at the same per-share price.

Output columns: date, ticker, type, quantity, price, acb
    `acb` is the running total ACB for that ticker AFTER the transaction,
    quantized to cents using banker's rounding.

v1 simplifications (intentional, see plan):
  - No commissions / outlays.
  - Only START, BUY, and SELL (no DRIP, ROC, splits, phantom distributions).
  - No superficial-loss rule.
  - No zero-floor handling; over-selling raises a clear ValueError.
  - Single currency (CAD); no FX.
  - Same-date tie-break is input file order.
"""

import argparse
import csv
import sys
from decimal import Decimal, ROUND_HALF_EVEN

CENTS = Decimal("0.01")
OUTPUT_COLUMNS = ["date", "ticker", "type", "quantity", "price", "acb"]


def load_transactions(paths):
    """Read all input CSVs, normalize, and return a list of dict rows."""
    rows = []
    for path in paths:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                rows.append({
                    "date": row["date"].strip(),
                    "ticker": row["ticker"].strip().upper(),
                    "type": row["type"].strip().upper(),
                    "quantity": Decimal(row["quantity"]),
                    "price": Decimal(row["price"]),
                })
    return rows


def compute_acb(rows):
    """Walk transactions in chronological order, yielding output dicts.

    Per-ticker state is (shares, total_acb), both Decimal.
    """
    holdings = {}  # ticker -> [shares, total_acb]
    # Stable sort by date; ties keep original input order.
    ordered = sorted(enumerate(rows), key=lambda p: (p[1]["date"], p[0]))

    for _, tx in ordered:
        ticker, tx_type = tx["ticker"], tx["type"]
        qty, price = tx["quantity"], tx["price"]

        # A START is only valid as the first appearance of its ticker.
        # This single check covers both ordering ("START came after a
        # BUY/SELL") and uniqueness ("two STARTs for one ticker").
        if tx_type == "START" and ticker in holdings:
            raise ValueError(
                f"START for {ticker} on {tx['date']} must precede "
                f"other transactions for that ticker"
            )

        state = holdings.setdefault(ticker, [Decimal(0), Decimal(0)])
        shares, total_acb = state

        if tx_type in ("START", "BUY"):
            shares += qty
            total_acb += qty * price
        elif tx_type == "SELL":
            if qty > shares:
                raise ValueError(
                    f"SELL of {ticker} on {tx['date']} exceeds holdings"
                )
            # CRA average-cost rule: per-share ACB unchanged by a sell.
            acb_per_share = total_acb / shares
            total_acb -= qty * acb_per_share
            shares -= qty
        else:
            raise ValueError(f"Unknown transaction type: {tx_type!r}")

        state[0], state[1] = shares, total_acb
        yield {
            "date": tx["date"],
            "ticker": ticker,
            "type": tx_type,
            "quantity": qty,
            "price": price,
            "acb": total_acb.quantize(CENTS, rounding=ROUND_HALF_EVEN),
        }


def write_csv(rows, out):
    writer = csv.DictWriter(out, fieldnames=OUTPUT_COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("inputs", nargs="+", help="input transaction CSV(s)")
    parser.add_argument("-o", "--output", help="output CSV (default: stdout)")
    args = parser.parse_args()

    rows = load_transactions(args.inputs)
    output_rows = compute_acb(rows)

    if args.output:
        with open(args.output, "w", newline="") as f:
            write_csv(output_rows, f)
    else:
        write_csv(output_rows, sys.stdout)


if __name__ == "__main__":
    main()

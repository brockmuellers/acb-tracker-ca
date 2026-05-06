"""ACB (Adjusted Cost Base) calculator for Canadian CRA tax reporting.

Reads one or more CSVs of transactions and emits a CSV with a running ACB
column per ticker, in chronological order.

Input columns (header required, order flexible):
    date           ISO 8601 (YYYY-MM-DD)
    ticker         string (normalized to upper-case)
    type           START, BUY, or SELL (case-insensitive)
    quantity       decimal, positive
    price          per-share price in `currency`, decimal, positive
    currency       OPTIONAL ISO 4217 code (default CAD, case-insensitive)
    exchange_rate  OPTIONAL foreign-currency-to-CAD rate.
                   Required for non-CAD rows; ignored on CAD rows
                   (always treated as 1).

A START row declares an opening balance for a ticker — `quantity` is the
shares already held and `price` is their per-share ACB. A ticker may have
at most one START, and it must come before any BUY/SELL for that ticker
chronologically (the run errors clearly otherwise). Mathematically a
START is identical to a BUY at the same per-share price.

Output columns: date, ticker, type, quantity, price, currency,
exchange_rate, price_cad, acb
    `price_cad` is `price * exchange_rate`, the per-share price in CAD
    used for the ACB math (raw Decimal product, not quantized).
    `acb` is the running total ACB for that ticker AFTER the transaction,
    always in CAD, quantized to cents using banker's rounding.

v1 simplifications (intentional, see plan):
  - No commissions / outlays.
  - Only START, BUY, and SELL (no DRIP, ROC, splits, phantom distributions).
  - No superficial-loss rule.
  - No zero-floor handling; over-selling raises a clear ValueError.
  - ACB is always in CAD; the user supplies the per-row foreign-to-CAD
    exchange rate. There is no FX rate file lookup, no auto-inversion of
    pairs, and no cross-currency chaining.
  - Same-date tie-break is input file order.
"""

import argparse
import csv
import sys
from decimal import ROUND_HALF_EVEN, Decimal

from tabulate import tabulate

CENTS = Decimal("0.01")
ONE = Decimal("1")
OUTPUT_COLUMNS = [
    "date", "ticker", "type", "quantity", "price",
    "currency", "exchange_rate", "amount_cad", "acb",
]


def load_transactions(paths):
    """Read all input CSVs, normalize, and return a list of dict rows."""
    rows = []
    for path in paths:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                currency = ((row.get("currency") or "").strip().upper()
                            or "CAD")
                rate_raw = (row.get("exchange_rate") or "").strip()
                if currency == "CAD":
                    exchange_rate = ONE
                else:
                    if not rate_raw:
                        raise ValueError(
                            f"{row['ticker'].strip().upper()} on "
                            f"{row['date'].strip()}: non-CAD currency "
                            f"{currency!r} requires an exchange_rate"
                        )
                    exchange_rate = Decimal(rate_raw)
                rows.append({
                    "date": row["date"].strip(),
                    "ticker": row["ticker"].strip().upper(),
                    "type": row["type"].strip().upper(),
                    "quantity": Decimal(row["quantity"]),
                    "price": Decimal(row["price"]),
                    "currency": currency,
                    "exchange_rate": exchange_rate,
                })
    return rows


def compute_acb(rows):
    """Walk transactions in chronological order, yielding output dicts.

    Per-ticker state is (shares, total_acb), both Decimal and always CAD.
    """
    holdings = {}  # ticker -> [shares, total_acb]
    seen_ticker_dates = set()
    # Stable sort by date; ties keep original input order.
    ordered = sorted(enumerate(rows), key=lambda p: (p[1]["date"], p[0]))

    for _, tx in ordered:
        key = (tx["ticker"], tx["date"])
        if key in seen_ticker_dates:
            print(f"Note: multiple transactions for {tx['ticker']} on {tx['date']}", flush=True)
        seen_ticker_dates.add(key)
        ticker, tx_type = tx["ticker"], tx["type"]
        qty, price = tx["quantity"], tx["price"]
        currency, rate = tx["currency"], tx["exchange_rate"]
        # TODO: double check this, but I think it's most accurate to calculate and round
        # the "amount" before doing the CAD conversion,since the exact dollar value paid
        # is more important than the precise share*price.
        # It may be cleaner to include the rounded amount in the input instead.
        amount_usd = (price * qty).quantize(CENTS, rounding=ROUND_HALF_EVEN)
        amount_cad = amount_usd * rate

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
            total_acb += amount_cad
        elif tx_type == "SELL":
            if qty > shares:
                raise ValueError(
                    f"SELL of {ticker} on {tx['date']} exceeds holdings: {qty} > {shares}"
                )
            # CRA average-cost rule: per-share ACB unchanged by a sell.
            acb_per_share = total_acb / shares
            total_acb -= qty * acb_per_share
            shares -= qty
        else:
            raise ValueError(f"Unknown transaction type: {tx_type!r}")

        # TODO: should total_acb be rounded when saving to state?
        state[0], state[1] = shares, total_acb
        yield {
            "date": tx["date"],
            "ticker": ticker,
            "type": tx_type,
            "quantity": qty,
            "price": price,
            "currency": currency,
            "exchange_rate": rate,
            "amount_cad": amount_cad,
            "acb": total_acb.quantize(CENTS, rounding=ROUND_HALF_EVEN),
        }


def write_csv(rows, out):
    writer = csv.DictWriter(out, fieldnames=OUTPUT_COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # TODO: optionally accept a directory, processing all files in it in alphabetical order.
    parser.add_argument("inputs", nargs="+", help="input transaction CSV(s)")
    parser.add_argument("-o", "--output", help="output CSV (default: stdout)")
    parser.add_argument("-p", "--pretty", action="store_true", help="pretty-print console output")
    args = parser.parse_args()

    rows = load_transactions(args.inputs)
    output_rows = compute_acb(rows)

    if args.output:
        with open(args.output, "w", newline="") as f:
            write_csv(output_rows, f)
    elif args.pretty:
        print(tabulate(output_rows, headers="keys", tablefmt="grid", floatfmt="s"))
    else:
        write_csv(output_rows, sys.stdout)


if __name__ == "__main__":
    main()

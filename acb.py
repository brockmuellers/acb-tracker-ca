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
    time              OPTIONAL H:MM or HH:MM (e.g. 8:00, 15:30).  Used as a
                      tiebreaker when the same ticker has multiple transactions
                      on the same date.  All transactions in such a group must
                      have a time, or a warning is shown.
    superficial_qty   OPTIONAL decimal >= 0.  Number of shares whose loss is
                      denied under the CRA superficial loss rule (i.e. shares
                      repurchased within 30 days before/after this SELL).
                      The denied loss is added back to the remaining ACB pool
                      so it defers into future per-share cost.  Set to 0 to
                      explicitly confirm no superficial loss and silence the
                      warning.  Absent: a warning is printed for any SELL that
                      realizes a loss.  Only valid on SELL rows with a loss.

A START row declares an opening balance for a ticker — `quantity` is the
shares already held and `price` is their per-share ACB. A ticker may have
at most one START, and it must come before any BUY/SELL for that ticker
chronologically (the run errors clearly otherwise). Mathematically a
START is identical to a BUY at the same per-share price.

Output columns: date, ticker, type, quantity, price, currency,
exchange_rate, amount_cad, acb_cad, gain_loss_cad, superficial_loss_cad
    `amount_cad` is `price * quantity * exchange_rate`, the total transaction
    amount in CAD used for the ACB math (quantized to cents before CAD
    conversion).
    `acb_cad` is the running total ACB for that ticker AFTER the transaction,
    always in CAD, quantized to cents using banker's rounding.
    `gain_loss_cad` is the realized (non-denied) capital gain or loss on SELL
    rows, in CAD, quantized to cents.  Zero for a fully superficial loss.
    `superficial_loss_cad` is the denied loss amount for rows where
    superficial_qty > 0, in CAD, quantized to cents.  Empty otherwise.

v1 simplifications (intentional, see plan):
  - No commissions / outlays.
  - Only START, BUY, and SELL (no DRIP, ROC, splits, phantom distributions).
  - Superficial loss rule is user-directed via superficial_qty; automatic
    30-day window detection is not supported (CRA affiliated-person rules
    make this impossible to determine from a single account's CSV).
  - No zero-floor handling; over-selling raises a clear ValueError.
  - ACB is always in CAD; the user supplies the per-row foreign-to-CAD
    exchange rate. There is no FX rate file lookup, no auto-inversion of
    pairs, and no cross-currency chaining.
"""

import argparse
import sys

from tabulate import tabulate

from acb_lib import compute_acb, load_transactions, write_csv


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
    if args.pretty:
        print(tabulate(output_rows, headers="keys", tablefmt="grid", floatfmt="s"))
    elif not args.output:
        write_csv(output_rows, sys.stdout)


if __name__ == "__main__":
    main()

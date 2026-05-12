"""Translate a brokerage CSV export into the normalized format expected by acb.py.

Usage:
    python3 translate.py <input_csv> <mapping_config> [-o <output_path>]
                         [--start YYYY-MM-DD] [--end YYYY-MM-DD]
                         [--fx-dir DIR]

If -o is omitted the output goes to temp/<input_stem>_translated.csv.

The mapping config is a YAML file with the following schema:

    column_map:               # REQUIRED — broker column name → acb column name
      Trade Date: date
      Ticker: ticker
      Action: type
      Shares: quantity
      Price ($): price
      Currency: currency

    optional_columns:         # optional — broker column names from column_map that may be absent from the CSV
      - Time                  #   no error is raised if these columns are not present in the input

    type_map:                 # optional — remap broker type values to BUY/SELL/START/TRANSFER
      Buy: BUY
      Sell: SELL

    skip_types:               # optional — broker type values to drop
      - Dividend
      - Reinvestment

    date_format: "%m/%d/%Y"   # optional — strptime format; omit if already ISO 8601

    defaults:                 # optional — static values for missing or empty columns
      currency: CAD
      cash_ticker: CASH-USD   # required when cash_type_map is used; ticker assigned to all cash rows

    settlement_fund_types:    # optional — broker settlement fund types whose quantity/price live in alternate columns
      types:
        - Sweep in
        - Sweep out
      quantity_col: Net Amount  # broker column to read quantity from (sign stripped)
      price_override: "1.0"    # literal price (omit to use mapped price column)

    cash_type_map:            # optional — broker types that generate a CASH-IN or CASH-OUT row
      quantity_col: Amount    #   broker column holding the cash amount (sign stripped)
                              #   cash row always uses: ticker=cash_ticker, price=1.0, quantity=abs(amount)
      types:                  #   ALWAYS-CASH: these types always emit a cash row
        Cash Dividend: CASH-IN  #   type only here → cash row only
        Advisor Fee: CASH-OUT
        Sell: CASH-IN           #   type also in type_map → security row + cash row (dual emission)
      ticker_fallback_types:  #   TICKER-CONDITIONAL: routing depends on whether the broker row has a ticker
        Security Transfer: CASH-IN  #   non-empty ticker → security row only (via type_map)
                                    #   empty ticker    → cash row only (the security is actually cash)

Only columns listed in column_map are kept; all other broker columns are dropped.
Required acb.py input columns: date, ticker, type, quantity, price.
Optional: currency, exchange_rate.

Exchange rates (--fx-dir):
    For non-CAD rows, acb.py requires an exchange_rate column. Pass --fx-dir pointing to a
    directory of Bank of Canada daily FX rate CSVs and the rate will be filled in automatically.
    Download files from: https://www.bankofcanada.ca/rates/exchange/daily-exchange-rates-lookup/
    Bank of Canada files cover business days only; if a transaction falls on a weekend or holiday
    the previous business day's rate is used. Multiple files for the same currency are merged.
"""

import argparse
import sys
from pathlib import Path

import yaml

from translate_lib import (
    ACB_INPUT_COLUMNS,
    ConfigurationError,
    FXRateError,
    TranslationError,
    load_fx_rates,
    translate_file,
    write_csv,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input_csv", help="brokerage export CSV")
    parser.add_argument("mapping_config", help="YAML mapping config")
    parser.add_argument("-o", "--output", help="output CSV (default: temp/<stem>_translated.csv)")
    parser.add_argument("--start", metavar="YYYY-MM-DD", help="exclude rows before this date (inclusive)")
    parser.add_argument("--end", metavar="YYYY-MM-DD", help="exclude rows after this date (inclusive)")
    parser.add_argument("--fx-dir", metavar="DIR", help="directory of Bank of Canada FX rate CSVs for automatic exchange rate lookup")
    parser.add_argument("--account", metavar="NUMBER", help="account number to stamp on every output row")
    args = parser.parse_args()

    fx_rates = None
    if args.fx_dir:
        try:
            fx_rates = load_fx_rates(args.fx_dir)
        except FXRateError as e:
            print(f"Exchange rate error: {e}", file=sys.stderr)
            sys.exit(1)

    try:
        translated = translate_file(
            args.input_csv, args.mapping_config,
            start=args.start, end=args.end, fx_rates=fx_rates,
        )
    except (ConfigurationError, yaml.YAMLError, OSError) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)
    except TranslationError as e:
        print(f"Translation error: {e}", file=sys.stderr)
        sys.exit(1)
    except FXRateError as e:
        print(f"Exchange rate error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.account:
        translated = [{**row, "account_number": args.account} for row in translated]

    present = set()
    for row in translated:
        present.update(row.keys())
    out_columns = [c for c in ACB_INPUT_COLUMNS if c in present]

    if args.output:
        with open(args.output, "w", newline="") as f:
            write_csv(translated, f, out_columns)
    else:
        stem = Path(args.input_csv).stem
        out_path = Path("temp") / f"{stem}_translated.csv"
        out_path.parent.mkdir(exist_ok=True)
        with open(out_path, "w", newline="") as f:
            write_csv(translated, f, out_columns)
        print(f"Written to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()

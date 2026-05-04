"""Translate a brokerage CSV export into the normalized format expected by acb.py.

Usage:
    python3 translate.py <input_csv> <mapping_config> [-o <output_path>]
                         [--start YYYY-MM-DD] [--end YYYY-MM-DD]
                         [--fx-dir DIR]

If -o is omitted the output goes to temp/<input_stem>_translated.csv.

The mapping config is a JSON file with the following schema:

    {
        "column_map": {           // REQUIRED — broker column name → acb column name
            "Trade Date": "date",
            "Ticker":     "ticker",
            "Action":     "type",
            "Shares":     "quantity",
            "Price ($)":  "price",
            "Currency":   "currency"
        },
        "type_map": {             // optional — remap broker type values to BUY/SELL/START
            "Buy":  "BUY",
            "Sell": "SELL"
        },
        "skip_types": ["Dividend", "Reinvestment"], // optional — broker type values to drop
        "date_format": "%m/%d/%Y", // optional — strptime format; omit if already ISO 8601
        "defaults": {              // optional — static values for missing or empty columns
            "currency": "CAD"
        },
        "sweep_types": {           // optional — broker transaction types whose quantity/price live in alternate columns
            "types":          ["Sweep in", "Sweep out"],
            "quantity_col":   "Net Amount",  // broker column to read quantity from (sign stripped)
            "price_override": "1.0"          // literal price string (omit to use mapped price column)
        }
    }

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
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ACB_INPUT_COLUMNS = ["date", "ticker", "type", "quantity", "price", "currency", "exchange_rate"]
REQUIRED_COLUMNS = ["date", "ticker", "type", "quantity", "price"]
FX_COL_RE = re.compile(r"^FX([A-Z]{3})CAD$")


def load_config(config_path):
    with open(config_path) as f:
        config = json.load(f)
    if "column_map" not in config or not config["column_map"]:
        raise ValueError(f"{config_path}: mapping config must have a non-empty 'column_map'")
    return config


def clean_number(s):
    """Strip leading $, commas, and whitespace from a numeric string."""
    return s.strip().lstrip("$").replace(",", "")


def translate_rows(rows, config):
    """Yield translated row dicts, one per input row."""
    column_map = config["column_map"]
    type_map = config.get("type_map", {})
    skip_types = set(config.get("skip_types", []))
    date_format = config.get("date_format")
    defaults = config.get("defaults", {})
    sweep_types_config = config.get("sweep_types", {})
    sweep_type_set = set(sweep_types_config.get("types", []))

    for row in rows:
        out = {}

        # Rename columns; drop anything not in column_map.
        for broker_col, acb_col in column_map.items():
            if broker_col in row:
                out[acb_col] = row[broker_col]

        # Drop rows whose raw type value is in skip_types.
        if skip_types and out.get("type") in skip_types:
            continue

        # Clean numeric fields; quantity is always positive (sign is encoded in type).
        for field in ("price", "quantity"):
            if field in out:
                out[field] = clean_number(out[field])
        if "quantity" in out:
            out["quantity"] = out["quantity"].lstrip("-")

        # Sweep type override: some brokers store quantity/price in alternate columns for
        # sweep transaction types (e.g. Vanguard puts dollar amount in Net Amount for sweeps).
        # Checked against the raw broker type value, before type_map remapping.
        if sweep_type_set and out.get("type") in sweep_type_set:
            if "quantity_col" in sweep_types_config:
                col = sweep_types_config["quantity_col"]
                if col not in row:
                    raise ValueError(
                        f"sweep_types quantity_col {col!r} not found in row"
                    )
                out["quantity"] = clean_number(row[col]).lstrip("-")
            if "price_override" in sweep_types_config:
                out["price"] = sweep_types_config["price_override"]

        # Remap type values.
        if type_map and "type" in out:
            raw = out["type"]
            if raw not in type_map:
                raise ValueError(
                    f"unknown type value {raw!r} — add it to type_map in your mapping config"
                )
            out["type"] = type_map[raw]

        # Convert date format to ISO 8601.
        if date_format and "date" in out:
            out["date"] = datetime.strptime(out["date"], date_format).strftime("%Y-%m-%d")

        # Fill defaults for missing or empty columns.
        for col, val in defaults.items():
            if not out.get(col):
                out[col] = val

        # Validate that quantity and price are non-zero after all transformations.
        # This is the primary safeguard against column mapping mis-configurations (e.g. a broker
        # that stores quantity in a non-standard column silently producing 0-share rows).
        loc = f"{out.get('ticker', '?')} on {out.get('date', '?')}"
        for field in ("quantity", "price"):
            if field in out:
                try:
                    val = float(out[field])
                except (ValueError, TypeError):
                    raise ValueError(f"{loc}: {field} {out[field]!r} is not a valid number")
                if val == 0:
                    raise ValueError(
                        f"{loc}: {field} is 0 — check your column_map or sweep_types config"
                    )

        yield out


def filter_by_date(rows, start, end):
    """Return only rows whose date falls within [start, end] (both inclusive, ISO 8601)."""
    return [
        r for r in rows
        if (start is None or r["date"] >= start)
        and (end is None or r["date"] <= end)
    ]


def validate_columns(rows):
    """Return list of translated rows; raise if any required column is missing."""
    collected = list(rows)
    if not collected:
        return collected
    present = set(collected[0].keys())
    for col in REQUIRED_COLUMNS:
        if col not in present:
            raise ValueError(
                f"missing required column {col!r} in translated output — "
                f"check your column_map"
            )
    return collected


def load_fx_rates(fx_dir):
    """Scan fx_dir for Bank of Canada FX CSVs; return {currency: {date: rate_str}}."""
    rates = {}
    for path in sorted(Path(fx_dir).glob("*.csv")):
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            fx_col = next((c for c in (reader.fieldnames or []) if FX_COL_RE.match(c)), None)
            if fx_col is None:
                raise ValueError(
                    f"{path.name}: no Bank of Canada FX column found "
                    f"(expected a column like FXUSDCAD)"
                )
            currency = FX_COL_RE.match(fx_col).group(1)
            currency_rates = rates.setdefault(currency, {})
            for row in reader:
                if row["date"] and row[fx_col]:
                    currency_rates[row["date"]] = row[fx_col]
    return rates


def lookup_rate(date_rates, currency, date):
    """Return rate string for date, falling back to the previous available date.

    Raises ValueError if date is beyond the end of the data or before it begins.
    """
    sorted_dates = sorted(date_rates)
    # Limitation: if the requested date is a weekend/holiday that falls exactly at the end of
    # the file's range (e.g. transaction on 2025-12-31 but file ends on 2025-12-30 because
    # 12/31 was a non-business day), this raises instead of falling back to 12/30. The fix
    # would require knowing the user's intended end date vs. the file's actual last entry.
    if date > sorted_dates[-1]:
        raise ValueError(
            f"no FX rate for '{currency}' on {date} — "
            f"latest available is {sorted_dates[-1]}; download a newer file"
        )
    candidates = [d for d in sorted_dates if d <= date]
    if not candidates:
        raise ValueError(
            f"no FX rate for '{currency}' on {date} — "
            f"earliest available is {sorted_dates[0]}; download an older file"
        )
    return date_rates[candidates[-1]]


def apply_fx_rates(rows, fx_rates):
    """Fill exchange_rate for non-CAD rows that don't already have one."""
    out = []
    for row in rows:
        row = dict(row)
        currency = row.get("currency", "CAD").upper()
        if currency != "CAD" and not row.get("exchange_rate"):
            if currency not in fx_rates:
                raise ValueError(
                    f"no FX rate data for '{currency}' — "
                    f"add a Bank of Canada FX{currency}CAD file to the --fx-dir directory"
                )
            row["exchange_rate"] = lookup_rate(fx_rates[currency], currency, row["date"])
        out.append(row)
    return out


def write_csv(rows, out, columns):
    writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input_csv", help="brokerage export CSV")
    parser.add_argument("mapping_config", help="JSON mapping config")
    parser.add_argument("-o", "--output", help="output CSV (default: temp/<stem>_translated.csv)")
    parser.add_argument("--start", metavar="YYYY-MM-DD", help="exclude rows before this date (inclusive)")
    parser.add_argument("--end", metavar="YYYY-MM-DD", help="exclude rows after this date (inclusive)")
    parser.add_argument("--fx-dir", metavar="DIR", help="directory of Bank of Canada FX rate CSVs for automatic exchange rate lookup")
    args = parser.parse_args()

    config = load_config(args.mapping_config)

    with open(args.input_csv, newline="") as f:
        raw_rows = list(csv.DictReader(f))

    translated = validate_columns(translate_rows(raw_rows, config))
    translated = filter_by_date(translated, args.start, args.end)

    if args.fx_dir:
        fx_rates = load_fx_rates(args.fx_dir)
        translated = apply_fx_rates(translated, fx_rates)

    # Determine which ACB columns are actually present in the output.
    present = set()
    for row in translated:
        present.update(row.keys())
    out_columns = [c for c in ACB_INPUT_COLUMNS if c in present]

    if args.output:
        out_path = args.output
        with open(out_path, "w", newline="") as f:
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

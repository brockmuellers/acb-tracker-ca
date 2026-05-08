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

    type_map:                 # optional — remap broker type values to BUY/SELL/START
      Buy: BUY
      Sell: SELL

    skip_types:               # optional — broker type values to drop
      - Dividend
      - Reinvestment

    date_format: "%m/%d/%Y"   # optional — strptime format; omit if already ISO 8601

    defaults:                 # optional — static values for missing or empty columns
      currency: CAD

    sweep_types:              # optional — broker sweep fund types whose quantity/price live in alternate columns
      types:
        - Sweep in
        - Sweep out
      quantity_col: Net Amount  # broker column to read quantity from (sign stripped)
      price_override: "1.0"    # literal price (omit to use mapped price column)

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
import re
import sys

import yaml
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ACB_INPUT_COLUMNS = ["date", "ticker", "type", "quantity", "price", "currency", "exchange_rate", "time", "superficial_qty"]
REQUIRED_COLUMNS = ["date", "ticker", "type", "quantity", "price"]
FX_COL_RE = re.compile(r"^FX([A-Z]{3})CAD$")

_VALID_CONFIG_KEYS = frozenset({"column_map", "type_map", "skip_types", "date_format", "defaults", "sweep_types"})
_VALID_SWEEP_KEYS = frozenset({"types", "quantity_col", "price_override"})


class ConfigurationError(ValueError):
    """Raised when the JSON mapping config is invalid."""


class TranslationError(ValueError):
    """Raised when a CSV row fails to translate."""


class FXRateError(ValueError):
    """Raised when an exchange rate cannot be found or loaded."""


@dataclass
class SweepConfig:
    types: set[str]
    quantity_col: str | None = None
    price_override: str | None = None

    @classmethod
    def from_dict(cls, data: dict, config_path: str) -> "SweepConfig":
        unknown = sorted(set(data) - _VALID_SWEEP_KEYS)
        if unknown:
            raise ConfigurationError(
                f"{config_path}: sweep_types has unknown key(s) {unknown}\n"
                f"Valid sweep_types keys: {sorted(_VALID_SWEEP_KEYS)}"
            )
        types = data.get("types")
        if not isinstance(types, list) or not types or not all(isinstance(x, str) for x in types):
            raise ConfigurationError(f"{config_path}: sweep_types.'types' must be a non-empty list of strings")
        for key in ("quantity_col", "price_override"):
            if key in data:
                val = data[key]
                if isinstance(val, (int, float)):
                    data[key] = str(val)
                elif not isinstance(val, str):
                    raise ConfigurationError(
                        f"{config_path}: sweep_types.'{key}' must be a string (got {type(val).__name__})"
                    )
        return cls(
            types=set(types),
            quantity_col=data.get("quantity_col"),
            price_override=data.get("price_override"),
        )


@dataclass
class AppConfig:
    column_map: dict[str, str]
    type_map: dict[str, str] = field(default_factory=dict)
    skip_types: set[str] = field(default_factory=set)
    date_format: str | None = None
    defaults: dict[str, str] = field(default_factory=dict)
    sweep: SweepConfig | None = None

    @classmethod
    def from_dict(cls, data: dict, config_path: str = "config") -> "AppConfig":
        """Parse and validate a raw config dict; raise ConfigurationError for any problem found."""
        p = config_path

        unknown = sorted(set(data) - _VALID_CONFIG_KEYS)
        if unknown:
            raise ConfigurationError(
                f"{p}: unknown key(s) {unknown}\n"
                f"Valid keys: {sorted(_VALID_CONFIG_KEYS)}"
            )

        cm = data.get("column_map")
        if not cm or not isinstance(cm, dict):
            raise ConfigurationError(
                f"{p}: 'column_map' must be a non-empty dict mapping broker column names to ACB column names"
            )
        for k, v in cm.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ConfigurationError(
                    f"{p}: 'column_map' keys and values must be strings (got {k!r}: {v!r})"
                )

        if "type_map" in data:
            tm = data["type_map"]
            if not isinstance(tm, dict):
                raise ConfigurationError(f"{p}: 'type_map' must be a dict (got {type(tm).__name__})")
            for k, v in tm.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    raise ConfigurationError(
                        f"{p}: 'type_map' keys and values must be strings (got {k!r}: {v!r})"
                    )

        if "skip_types" in data:
            st = data["skip_types"]
            if not isinstance(st, list) or not all(isinstance(x, str) for x in st):
                raise ConfigurationError(f"{p}: 'skip_types' must be a list of strings (got {type(st).__name__})")

        if "date_format" in data:
            df = data["date_format"]
            if not isinstance(df, str):
                raise ConfigurationError(f"{p}: 'date_format' must be a string (got {type(df).__name__})")

        if "defaults" in data:
            d = data["defaults"]
            if not isinstance(d, dict):
                raise ConfigurationError(f"{p}: 'defaults' must be a dict (got {type(d).__name__})")
            for k, v in d.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    raise ConfigurationError(
                        f"{p}: 'defaults' keys and values must be strings (got {k!r}: {v!r})"
                    )

        sweep = None
        if "sweep_types" in data:
            sw = data["sweep_types"]
            if not isinstance(sw, dict):
                raise ConfigurationError(f"{p}: 'sweep_types' must be a dict (got {type(sw).__name__})")
            sweep = SweepConfig.from_dict(sw, config_path)

        return cls(
            column_map=cm,
            type_map=data.get("type_map", {}),
            skip_types=set(data.get("skip_types", [])),
            date_format=data.get("date_format"),
            defaults=data.get("defaults", {}),
            sweep=sweep,
        )


def load_config(config_path: str) -> AppConfig:
    with open(config_path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ConfigurationError(f"{config_path}: mapping config must be a YAML mapping")
    return AppConfig.from_dict(data, config_path)


def validate_column_map(column_map, csv_headers, config_path="config", sweep_quantity_col=None):
    """Raise if any column_map key (or sweep_types.quantity_col) is absent from the CSV headers."""
    csv_col_set = set(csv_headers)
    missing = [k for k in column_map if k not in csv_col_set]
    if sweep_quantity_col and sweep_quantity_col not in csv_col_set:
        missing.append(f"{sweep_quantity_col} (sweep_types.quantity_col)")
    if missing:
        raise ConfigurationError(
            f"{config_path}: columns not found in CSV: {missing}\n"
            f"Available columns: {sorted(csv_col_set)}"
        )


def clean_number(s):
    """Strip $, commas, and whitespace from a numeric string, preserving leading minus sign."""
    s = s.strip().replace(",", "").replace("$", "")
    return s


def translate_rows(rows, config: AppConfig):
    """Yield translated row dicts, one per input row."""
    column_map = config.column_map
    type_map = config.type_map
    skip_types = config.skip_types
    date_format = config.date_format
    defaults = config.defaults
    sweep = config.sweep

    # start=2: DictReader consumed row 1 as the header, so first data row is spreadsheet row 2
    for row_num, row in enumerate(rows, start=2):
        out = {}

        # Rename columns; drop anything not in column_map.
        for broker_col, acb_col in column_map.items():
            if broker_col in row:
                out[acb_col] = row[broker_col]

        # Raise early if the type column is present but blank.
        if "type" in out and not out["type"].strip():
            raise TranslationError(
                f"row {row_num}: 'type' column is blank — check the column mapped to 'type' in column_map"
            )

        # Drop rows whose raw type value is in skip_types.
        if skip_types and out.get("type") in skip_types:
            continue

        # Clean numeric fields; quantity is always positive (sign is encoded in type).
        for col in ("price", "quantity"):
            if col in out:
                out[col] = clean_number(out[col])
        if "quantity" in out:
            out["quantity"] = out["quantity"].lstrip("-")

        # Sweep type override: some brokers store quantity/price in alternate columns for
        # sweep transaction types (e.g. Vanguard puts dollar amount in Net Amount for sweeps).
        # Checked against the raw broker type value, before type_map remapping.
        if sweep and out.get("type") in sweep.types:
            if sweep.quantity_col:
                col = sweep.quantity_col
                if col not in row:
                    raise TranslationError(
                        f"row {row_num}: sweep_types quantity_col {col!r} not found in row"
                    )
                out["quantity"] = clean_number(row[col]).lstrip("-")
            if sweep.price_override:
                out["price"] = sweep.price_override

        # Remap type values.
        if type_map and "type" in out:
            raw = out["type"]
            if raw not in type_map:
                raise TranslationError(
                    f"row {row_num}: unknown type value {raw!r} — add it to type_map in your mapping config"
                )
            out["type"] = type_map[raw]

        # Convert date format to ISO 8601.
        if date_format and "date" in out:
            raw_date = out["date"]
            try:
                out["date"] = datetime.strptime(raw_date, date_format).strftime("%Y-%m-%d")
            except ValueError:
                raise TranslationError(
                    f"row {row_num} ({out.get('ticker', '?')}): date {raw_date!r} does not match "
                    f"format {date_format!r} (hint: use %Y for 4-digit year, %y for 2-digit)"
                )

        # Fill defaults for missing or empty columns.
        for col, val in defaults.items():
            if not out.get(col):
                out[col] = val

        # Validate that quantity and price are non-zero after all transformations.
        # This is the primary safeguard against column mapping mis-configurations (e.g. a broker
        # that stores quantity in a non-standard column silently producing 0-share rows).
        loc = f"row {row_num} ({out.get('ticker', '?')} on {out.get('date', '?')})"
        for col in ("quantity", "price"):
            if col in out:
                try:
                    val = float(out[col])
                except (ValueError, TypeError):
                    raise TranslationError(f"{loc}: {col} {out[col]!r} is not a valid number")
                if val == 0:
                    raise TranslationError(
                        f"{loc}: {col} is 0 — check your column_map or sweep_types config"
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
            raise ConfigurationError(
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
                raise FXRateError(
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

    Raises FXRateError if date is beyond the end of the data or before it begins.
    """
    sorted_dates = sorted(date_rates)
    # Limitation: if the requested date is a weekend/holiday that falls exactly at the end of
    # the file's range (e.g. transaction on 2025-12-31 but file ends on 2025-12-30 because
    # 12/31 was a non-business day), this raises instead of falling back to 12/30. The fix
    # would require knowing the user's intended end date vs. the file's actual last entry.
    if date > sorted_dates[-1]:
        raise FXRateError(
            f"no FX rate for '{currency}' on {date} — "
            f"latest available is {sorted_dates[-1]}; download a newer file"
        )
    candidates = [d for d in sorted_dates if d <= date]
    if not candidates:
        raise FXRateError(
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
                raise FXRateError(
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

    try:
        config = load_config(args.mapping_config)
    except (ConfigurationError, yaml.YAMLError, OSError) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(args.input_csv, newline="") as f:
            raw_rows = list(csv.DictReader(f))
    except OSError as e:
        print(f"Input error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        if raw_rows:
            sweep_quantity_col = config.sweep.quantity_col if config.sweep else None
            validate_column_map(
                config.column_map,
                raw_rows[0].keys(),
                config_path=args.mapping_config,
                sweep_quantity_col=sweep_quantity_col,
            )
        translated = validate_columns(translate_rows(raw_rows, config))
        translated = filter_by_date(translated, args.start, args.end)
    except (ConfigurationError, TranslationError) as e:
        print(f"Translation error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.fx_dir:
        try:
            fx_rates = load_fx_rates(args.fx_dir)
            translated = apply_fx_rates(translated, fx_rates)
        except FXRateError as e:
            print(f"Exchange rate error: {e}", file=sys.stderr)
            sys.exit(1)

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

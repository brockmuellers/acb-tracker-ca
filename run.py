"""Run the full ACB pipeline from a YAML config file.

Usage:
    python3 run.py <config.yaml> [-o output.csv] [-p]

The config file specifies brokerage sources to translate and combine:

    fx_dir: fx_rates/              # optional — directory of Bank of Canada FX CSVs
    output: output/combined.csv    # optional — output path (relative to config file)
    start: 2025-01-01              # optional — exclude rows before this date (inclusive)
    end: 2025-12-31                # optional — exclude rows after this date (inclusive)

    sources:
      - input: exports/starting.csv
        mapping: mappings/manual.yaml

      - input: exports/vanguard/*.csv
        mapping: mappings/vanguard.yaml

      - input: exports/schwab/*.csv
        mapping: mappings/schwab.yaml

All paths are relative to the config file's directory.
Sources are processed in order; glob patterns are expanded alphabetically.
The -o flag is relative to the current working directory and overrides 'output'.
"""

import argparse
import glob as glob_module
import sys
from pathlib import Path

import yaml
from tabulate import tabulate

from acb_lib import GREEN, RESET, YELLOW, compute_acb, compute_holdings, normalize_rows, write_csv, write_holdings_csv
from check_holdings import diff_holdings, load_holdings
from translate_lib import (
    ConfigurationError,
    FXRateError,
    TranslationError,
    load_fx_rates,
    translate_file,
)

_VALID_RUN_KEYS = frozenset({"sources", "output", "output_holdings", "expected_holdings", "fx_dir", "start", "end"})
_VALID_SOURCE_KEYS = frozenset({"input", "mapping", "account_number"})


def load_run_config(config_path):
    """Load and validate the master run config YAML."""
    with open(config_path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ConfigurationError(f"{config_path}: run config must be a YAML mapping")

    unknown = sorted(set(data) - _VALID_RUN_KEYS)
    if unknown:
        raise ConfigurationError(
            f"{config_path}: unknown key(s) {unknown}\n"
            f"Valid keys: {sorted(_VALID_RUN_KEYS)}"
        )

    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ConfigurationError(f"{config_path}: 'sources' must be a non-empty list")

    for i, src in enumerate(sources):
        if not isinstance(src, dict):
            raise ConfigurationError(f"{config_path}: sources[{i}] must be a mapping")
        unknown_src = sorted(set(src) - _VALID_SOURCE_KEYS)
        if unknown_src:
            raise ConfigurationError(
                f"{config_path}: sources[{i}] has unknown key(s) {unknown_src}\n"
                f"Valid source keys: {sorted(_VALID_SOURCE_KEYS)}"
            )
        for key in ("input", "mapping"):
            if key not in src:
                raise ConfigurationError(
                    f"{config_path}: sources[{i}] missing required key '{key}'"
                )

    return data


def _resolve(base_dir, path_str):
    """Resolve a path string relative to base_dir if it is not absolute."""
    p = Path(path_str)
    return p if p.is_absolute() else base_dir / p


def _expand_input(base_dir, pattern_str):
    """Glob-expand an input pattern; return sorted list of Paths.

    Raises FileNotFoundError if no files match.
    """
    search = str(_resolve(base_dir, pattern_str))
    paths = sorted(Path(p) for p in glob_module.glob(search))
    if not paths:
        raise FileNotFoundError(f"no files matched: {search}")
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("config", help="run config YAML")
    parser.add_argument("-o", "--output", help="output CSV (overrides config; relative to CWD)")
    parser.add_argument("-p", "--pretty", action="store_true", help="pretty-print console output")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    base_dir = config_path.parent

    try:
        run_config = load_run_config(config_path)
    except (ConfigurationError, yaml.YAMLError, OSError) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    fx_rates = None
    if run_config.get("fx_dir"):
        try:
            fx_rates = load_fx_rates(_resolve(base_dir, run_config["fx_dir"]))
        except FXRateError as e:
            print(f"Exchange rate error: {e}", file=sys.stderr)
            sys.exit(1)

    # YAML parses bare dates (2025-01-01) as datetime.date; convert to ISO string.
    start = str(run_config["start"]) if run_config.get("start") is not None else None
    end = str(run_config["end"]) if run_config.get("end") is not None else None

    all_translated = []
    for i, src in enumerate(run_config["sources"]):
        try:
            input_paths = _expand_input(base_dir, src["input"])
        except FileNotFoundError as e:
            print(f"Input error (sources[{i}]): {e}", file=sys.stderr)
            sys.exit(1)

        mapping_path = str(_resolve(base_dir, src["mapping"]))

        for input_path in input_paths:
            try:
                rows = translate_file(
                    str(input_path), mapping_path,
                    start=start, end=end, fx_rates=fx_rates,
                )
                if src.get("account_number"):
                    acct = str(src["account_number"])
                    rows = [{**r, "account_number": acct} for r in rows]
                all_translated.extend(rows)
            except (ConfigurationError, yaml.YAMLError, OSError) as e:
                print(f"Configuration error ({input_path.name}): {e}", file=sys.stderr)
                sys.exit(1)
            except TranslationError as e:
                print(f"Translation error ({input_path.name}): {e}", file=sys.stderr)
                sys.exit(1)
            except FXRateError as e:
                print(f"Exchange rate error ({input_path.name}): {e}", file=sys.stderr)
                sys.exit(1)

    try:
        output_rows = list(compute_acb(normalize_rows(all_translated)))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        with open(args.output, "w", newline="") as f:
            write_csv(output_rows, f)
    elif run_config.get("output"):
        output_path = _resolve(base_dir, run_config["output"])
        with open(output_path, "w", newline="") as f:
            write_csv(output_rows, f)

    holdings_rows = None
    if run_config.get("output_holdings") or run_config.get("expected_holdings") or args.pretty:
        holdings_rows = compute_holdings(output_rows)

    if run_config.get("output_holdings"):
        holdings_path = _resolve(base_dir, run_config["output_holdings"])
        with open(holdings_path, "w", newline="") as f:
            write_holdings_csv(holdings_rows, f)

    if args.pretty:
        print("=== Transactions ===")
        print(tabulate(output_rows, headers="keys", tablefmt="grid", floatfmt="s"))
        print("\n=== Holdings ===")
        print(tabulate(holdings_rows, headers="keys", tablefmt="grid", floatfmt="s"))
    elif not args.output and not run_config.get("output"):
        write_csv(output_rows, sys.stdout)

    if run_config.get("expected_holdings"):
        expected_path = _resolve(base_dir, run_config["expected_holdings"])
        actual = {
            (r["account_number"], r["ticker"]): {
                "quantity": str(r["quantity"]),
                "acb_cad": str(r["acb_cad"]),
            }
            for r in holdings_rows
        }
        try:
            expected = load_holdings(str(expected_path))
        except OSError as e:
            print(f"Holdings check error: {e}", file=sys.stderr)
            sys.exit(1)
        diffs = diff_holdings(expected, actual)
        if diffs:
            print(f"{YELLOW}Holdings differ ({len(diffs)} difference(s)):{RESET}", file=sys.stderr)
            for line in diffs:
                print(line, file=sys.stderr)
            sys.exit(1)
        print(f"{GREEN}Holdings match.{RESET}")


if __name__ == "__main__":
    main()

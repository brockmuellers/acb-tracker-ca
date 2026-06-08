"""Web entry point for the ACB calculator, callable from Pyodide.

All I/O is text-based (no filesystem paths). ANSI color codes are stripped
from warnings before returning them so they display cleanly in HTML.
"""

import contextlib
import csv
import io
import json
import re
import sys

import yaml

from acb_lib import compute_acb, compute_holdings, normalize_rows
from translate_lib import (
    ConfigurationError,
    FXRateError,
    TranslationError,
    apply_fx_rates,
    filter_by_date,
    load_config_from_text,
    load_fx_rates_from_text,
    translate_rows,
    validate_column_map,
    validate_columns,
)

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _strip_ansi(s):
    return _ANSI_RE.sub("", s)


def run_pipeline(sources_json, fx_csv_text=None, start=None, end=None):
    """Run the full translate → ACB → holdings pipeline from in-memory text.

    Args:
        sources_json: JSON string of a list of objects:
            [{csv_text, mapping_yaml_text, mapping_name?, account_number?}, ...]
        fx_csv_text: optional Bank of Canada FX CSV as a string (single currency file)
        start: optional ISO date string, inclusive lower bound (e.g. "2024-01-01")
        end:   optional ISO date string, inclusive upper bound (e.g. "2024-12-31")

    Returns:
        JSON string: {output_rows, holdings_rows, warnings, error}
        output_rows and holdings_rows are lists of string-valued dicts.
        error is null on success, or a string describing the failure.
        warnings is a (possibly empty) string with newline-separated warning messages.
    """
    # Pyodide passes JS null as a JsNull proxy rather than Python None.
    fx_csv_text = None if not fx_csv_text else str(fx_csv_text)
    start = None if not start else str(start)
    end = None if not end else str(end)

    warnings_buf = io.StringIO()

    try:
        sources = json.loads(sources_json)

        fx_rates = None
        if fx_csv_text and fx_csv_text.strip():
            fx_rates = load_fx_rates_from_text(fx_csv_text)

        all_translated = []
        with contextlib.redirect_stderr(warnings_buf):
            for src in sources:
                csv_text = src["csv_text"]
                mapping_yaml_text = src["mapping_yaml_text"]
                mapping_name = src.get("mapping_name", "config")
                account_number = src.get("account_number", "")

                config = load_config_from_text(mapping_yaml_text, mapping_name)
                raw_rows = list(csv.DictReader(io.StringIO(csv_text)))

                if raw_rows:
                    sweep_qty_col = config.sweep.quantity_col if config.sweep else None
                    cash_qty_col = config.cash_type_map.quantity_col if config.cash_type_map else None
                    validate_column_map(
                        config.column_map, raw_rows[0].keys(), mapping_name,
                        sweep_qty_col, cash_qty_col, config.optional_columns,
                    )

                rows = validate_columns(translate_rows(raw_rows, config))
                rows = filter_by_date(rows, start, end)
                if fx_rates:
                    rows = apply_fx_rates(rows, fx_rates)

                if account_number:
                    rows = [{**r, "account_number": account_number} for r in rows]

                all_translated.extend(rows)

            output_rows = list(compute_acb(normalize_rows(all_translated)))

        holdings_rows = compute_holdings(output_rows)

        def _serialize(row):
            return {k: str(v) if v != "" else "" for k, v in row.items()}

        return json.dumps({
            "output_rows": [_serialize(r) for r in output_rows],
            "holdings_rows": [_serialize(r) for r in holdings_rows],
            "warnings": _strip_ansi(warnings_buf.getvalue()).strip(),
            "error": None,
        })

    except (ConfigurationError, TranslationError, FXRateError, ValueError, yaml.YAMLError) as e:
        return json.dumps({
            "output_rows": [],
            "holdings_rows": [],
            "warnings": _strip_ansi(warnings_buf.getvalue()).strip(),
            "error": str(e),
        })

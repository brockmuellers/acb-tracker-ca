"""Tests for translate.py."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from translate import (
    apply_fx_rates,
    clean_number,
    filter_by_date,
    load_config,
    load_fx_rates,
    lookup_rate,
    translate_rows,
    validate_columns,
)

REPO = Path(__file__).parent

BASE_CONFIG = {
    "column_map": {
        "Trade Date": "date",
        "Symbol": "ticker",
        "Txn Type": "type",
        "Shares": "quantity",
        "Price ($)": "price",
    }
}


def make_row(**kwargs):
    return {
        "Trade Date": "2024-01-15",
        "Symbol": "VFV",
        "Txn Type": "BUY",
        "Shares": "100",
        "Price ($)": "98.50",
        "Ignored Column": "drop me",
        **kwargs,
    }


# --- clean_number ---

def test_clean_number_strips_dollar():
    assert clean_number("$98.50") == "98.50"


def test_clean_number_strips_commas():
    assert clean_number("1,234.56") == "1234.56"


def test_clean_number_strips_both():
    assert clean_number("$1,234.56") == "1234.56"


def test_clean_number_strips_whitespace():
    assert clean_number("  98.50  ") == "98.50"


def test_clean_number_passthrough():
    assert clean_number("98.50") == "98.50"


# --- column_map ---

def test_column_renaming():
    rows = [make_row()]
    out = list(translate_rows(rows, BASE_CONFIG))
    assert out[0]["date"] == "2024-01-15"
    assert out[0]["ticker"] == "VFV"
    assert out[0]["type"] == "BUY"
    assert out[0]["quantity"] == "100"
    assert out[0]["price"] == "98.50"


def test_unmapped_columns_dropped():
    rows = [make_row()]
    out = list(translate_rows(rows, BASE_CONFIG))
    assert "Ignored Column" not in out[0]
    assert "Trade Date" not in out[0]


# --- type_map ---

def test_type_map_remaps_value():
    config = {**BASE_CONFIG, "type_map": {"Buy": "BUY", "Sell": "SELL"}}
    rows = [make_row(**{"Txn Type": "Buy"})]
    out = list(translate_rows(rows, config))
    assert out[0]["type"] == "BUY"


def test_type_map_unknown_value_raises():
    config = {**BASE_CONFIG, "type_map": {"Buy": "BUY"}}
    rows = [make_row(**{"Txn Type": "Dividend"})]
    with pytest.raises(ValueError, match="unknown type value 'Dividend'"):
        list(translate_rows(rows, config))


def test_no_type_map_passes_value_through():
    rows = [make_row(**{"Txn Type": "BUY"})]
    out = list(translate_rows(rows, BASE_CONFIG))
    assert out[0]["type"] == "BUY"


# --- date_format ---

def test_date_format_converts_to_iso():
    config = {**BASE_CONFIG, "date_format": "%m/%d/%Y"}
    rows = [make_row(**{"Trade Date": "01/15/2024"})]
    out = list(translate_rows(rows, config))
    assert out[0]["date"] == "2024-01-15"


def test_no_date_format_passes_date_through():
    rows = [make_row(**{"Trade Date": "2024-01-15"})]
    out = list(translate_rows(rows, BASE_CONFIG))
    assert out[0]["date"] == "2024-01-15"


# --- value cleaning ---

def test_dollar_stripped_from_price():
    rows = [make_row(**{"Price ($)": "$98.50"})]
    out = list(translate_rows(rows, BASE_CONFIG))
    assert out[0]["price"] == "98.50"


def test_commas_stripped_from_quantity():
    rows = [make_row(**{"Shares": "1,000"})]
    out = list(translate_rows(rows, BASE_CONFIG))
    assert out[0]["quantity"] == "1000"


# --- defaults ---

def test_defaults_fill_missing_column():
    config = {**BASE_CONFIG, "defaults": {"currency": "CAD"}}
    rows = [make_row()]
    out = list(translate_rows(rows, config))
    assert out[0]["currency"] == "CAD"


def test_defaults_fill_empty_column():
    config = {
        "column_map": {**BASE_CONFIG["column_map"], "Currency": "currency"},
        "defaults": {"currency": "CAD"},
    }
    rows = [make_row(**{"Currency": ""})]
    out = list(translate_rows(rows, config))
    assert out[0]["currency"] == "CAD"


def test_defaults_do_not_overwrite_existing_value():
    config = {
        "column_map": {**BASE_CONFIG["column_map"], "Currency": "currency"},
        "defaults": {"currency": "CAD"},
    }
    rows = [make_row(**{"Currency": "USD"})]
    out = list(translate_rows(rows, config))
    assert out[0]["currency"] == "USD"


# --- filter_by_date ---

DATED_ROWS = [
    {"date": "2024-01-01", "ticker": "VFV", "type": "BUY", "quantity": "10", "price": "100"},
    {"date": "2024-06-15", "ticker": "VFV", "type": "BUY", "quantity": "10", "price": "105"},
    {"date": "2024-12-31", "ticker": "VFV", "type": "SELL", "quantity": "5", "price": "110"},
]


def test_filter_start_excludes_earlier_rows():
    result = filter_by_date(DATED_ROWS, start="2024-06-01", end=None)
    assert [r["date"] for r in result] == ["2024-06-15", "2024-12-31"]


def test_filter_end_excludes_later_rows():
    result = filter_by_date(DATED_ROWS, start=None, end="2024-06-30")
    assert [r["date"] for r in result] == ["2024-01-01", "2024-06-15"]


def test_filter_both_bounds_inclusive():
    result = filter_by_date(DATED_ROWS, start="2024-01-01", end="2024-06-15")
    assert [r["date"] for r in result] == ["2024-01-01", "2024-06-15"]


def test_filter_no_bounds_returns_all():
    result = filter_by_date(DATED_ROWS, start=None, end=None)
    assert result == DATED_ROWS


def test_filter_empty_result():
    result = filter_by_date(DATED_ROWS, start="2025-01-01", end=None)
    assert result == []


def test_cli_start_flag_filters_rows(tmp_path):
    src = tmp_path / "broker.csv"
    src.write_text(
        "Trade Date,Symbol,Txn Type,Shares,Price ($)\n"
        "2024-01-15,VFV,BUY,100,98.50\n"
        "2024-03-10,VFV,BUY,50,102.00\n"
        "2024-06-20,VFV,SELL,75,110.00\n"
    )
    cfg = tmp_path / "map.json"
    cfg.write_text(json.dumps(BASE_CONFIG))
    out = tmp_path / "out.csv"
    subprocess.run(
        [sys.executable, str(REPO / "translate.py"), str(src), str(cfg),
         "-o", str(out), "--start", "2024-03-10"],
        check=True,
    )
    assert out.read_text() == (
        "date,ticker,type,quantity,price\n"
        "2024-03-10,VFV,BUY,50,102.00\n"
        "2024-06-20,VFV,SELL,75,110.00\n"
    )


def test_cli_end_flag_filters_rows(tmp_path):
    src = tmp_path / "broker.csv"
    src.write_text(
        "Trade Date,Symbol,Txn Type,Shares,Price ($)\n"
        "2024-01-15,VFV,BUY,100,98.50\n"
        "2024-03-10,VFV,BUY,50,102.00\n"
        "2024-06-20,VFV,SELL,75,110.00\n"
    )
    cfg = tmp_path / "map.json"
    cfg.write_text(json.dumps(BASE_CONFIG))
    out = tmp_path / "out.csv"
    subprocess.run(
        [sys.executable, str(REPO / "translate.py"), str(src), str(cfg),
         "-o", str(out), "--end", "2024-03-10"],
        check=True,
    )
    assert out.read_text() == (
        "date,ticker,type,quantity,price\n"
        "2024-01-15,VFV,BUY,100,98.50\n"
        "2024-03-10,VFV,BUY,50,102.00\n"
    )


def test_cli_start_and_end_flags_together(tmp_path):
    src = tmp_path / "broker.csv"
    src.write_text(
        "Trade Date,Symbol,Txn Type,Shares,Price ($)\n"
        "2024-01-15,VFV,BUY,100,98.50\n"
        "2024-03-10,VFV,BUY,50,102.00\n"
        "2024-06-20,VFV,SELL,75,110.00\n"
    )
    cfg = tmp_path / "map.json"
    cfg.write_text(json.dumps(BASE_CONFIG))
    out = tmp_path / "out.csv"
    subprocess.run(
        [sys.executable, str(REPO / "translate.py"), str(src), str(cfg),
         "-o", str(out), "--start", "2024-01-15", "--end", "2024-03-10"],
        check=True,
    )
    assert out.read_text() == (
        "date,ticker,type,quantity,price\n"
        "2024-01-15,VFV,BUY,100,98.50\n"
        "2024-03-10,VFV,BUY,50,102.00\n"
    )


# --- validation ---

def test_missing_required_column_raises():
    config = {
        "column_map": {
            "Trade Date": "date",
            "Symbol": "ticker",
            # "type" intentionally missing
            "Shares": "quantity",
            "Price ($)": "price",
        }
    }
    rows = [make_row()]
    with pytest.raises(ValueError, match="missing required column 'type'"):
        validate_columns(translate_rows(rows, config))


def test_validate_passes_with_all_required_columns():
    rows = [make_row()]
    result = validate_columns(translate_rows(rows, BASE_CONFIG))
    assert len(result) == 1


def test_validate_empty_input_returns_empty():
    result = validate_columns(translate_rows([], BASE_CONFIG))
    assert result == []


# --- load_config ---

def test_load_config_raises_without_column_map(tmp_path):
    cfg = tmp_path / "bad.json"
    cfg.write_text(json.dumps({"type_map": {"Buy": "BUY"}}))
    with pytest.raises(ValueError, match="column_map"):
        load_config(str(cfg))


def test_load_config_raises_with_empty_column_map(tmp_path):
    cfg = tmp_path / "bad.json"
    cfg.write_text(json.dumps({"column_map": {}}))
    with pytest.raises(ValueError, match="column_map"):
        load_config(str(cfg))


# --- CLI end-to-end ---

def test_cli_basic_translation(tmp_path):
    src = tmp_path / "broker.csv"
    src.write_text(
        "Trade Date,Symbol,Txn Type,Shares,Price ($),Extra\n"
        "2024-01-15,VFV,BUY,100,98.50,ignored\n"
        "2024-03-10,VFV,BUY,50,102.00,ignored\n"
    )
    cfg = tmp_path / "map.json"
    cfg.write_text(json.dumps(BASE_CONFIG))
    out = tmp_path / "out.csv"
    subprocess.run(
        [sys.executable, str(REPO / "translate.py"), str(src), str(cfg), "-o", str(out)],
        check=True,
    )
    assert out.read_text() == (
        "date,ticker,type,quantity,price\n"
        "2024-01-15,VFV,BUY,100,98.50\n"
        "2024-03-10,VFV,BUY,50,102.00\n"
    )


def test_cli_with_type_map_and_date_format(tmp_path):
    src = tmp_path / "broker.csv"
    src.write_text(
        "Date,Ticker,Action,Qty,Price\n"
        "01/15/2024,VFV,Buy,100,$98.50\n"
    )
    config = {
        "column_map": {
            "Date": "date",
            "Ticker": "ticker",
            "Action": "type",
            "Qty": "quantity",
            "Price": "price",
        },
        "type_map": {"Buy": "BUY", "Sell": "SELL"},
        "date_format": "%m/%d/%Y",
    }
    cfg = tmp_path / "map.json"
    cfg.write_text(json.dumps(config))
    out = tmp_path / "out.csv"
    subprocess.run(
        [sys.executable, str(REPO / "translate.py"), str(src), str(cfg), "-o", str(out)],
        check=True,
    )
    assert out.read_text() == (
        "date,ticker,type,quantity,price\n"
        "2024-01-15,VFV,BUY,100,98.50\n"
    )


def test_cli_wealthsimple_fixture(tmp_path):
    # Simulate a Wealthsimple Trade CSV export (stock trades subset).
    src = tmp_path / "ws_export.csv"
    src.write_text(
        "Date,Submitted Date,Filled Date,Status,Account,Action,Transaction Type,"
        "Ticker,Shares,Price,Total Value,Currency,Notes\n"
        "2025-01-02,2025-01-02,2025-01-02,Filled,TFSA,Buy,Market,"
        "AAPL,10,180.00,1800.00,USD,\n"
        "2025-01-15,2025-01-15,2025-01-15,Filled,TFSA,Buy,Market,"
        "VFV,100,98.50,9850.00,CAD,\n"
    )
    out = tmp_path / "out.csv"
    subprocess.run(
        [sys.executable, str(REPO / "translate.py"),
         str(src), str(REPO / "mappings" / "wealthsimple.json"),
         "-o", str(out)],
        check=True,
    )
    assert out.read_text() == (
        "date,ticker,type,quantity,price,currency\n"
        "2025-01-02,AAPL,BUY,10,180.00,USD\n"
        "2025-01-15,VFV,BUY,100,98.50,CAD\n"
    )


def test_cli_output_pipes_into_acb(tmp_path):
    # Full pipeline: broker CSV → translate → acb.py → check ACB output.
    src = tmp_path / "broker.csv"
    src.write_text(
        "Trade Date,Symbol,Txn Type,Shares,Price ($)\n"
        "2024-01-15,VFV,BUY,100,98.50\n"
        "2024-03-10,VFV,BUY,50,102.00\n"
        "2024-06-20,VFV,SELL,75,110.00\n"
    )
    cfg = tmp_path / "map.json"
    cfg.write_text(json.dumps(BASE_CONFIG))
    translated = tmp_path / "translated.csv"
    subprocess.run(
        [sys.executable, str(REPO / "translate.py"), str(src), str(cfg), "-o", str(translated)],
        check=True,
    )
    acb_out = tmp_path / "acb_out.csv"
    subprocess.run(
        [sys.executable, str(REPO / "acb.py"), str(translated), "-o", str(acb_out)],
        check=True,
    )
    assert acb_out.read_text() == (
        "date,ticker,type,quantity,price,currency,exchange_rate,price_cad,acb\n"
        "2024-01-15,VFV,BUY,100,98.50,CAD,1,98.50,9850.00\n"
        "2024-03-10,VFV,BUY,50,102.00,CAD,1,102.00,14950.00\n"
        "2024-06-20,VFV,SELL,75,110.00,CAD,1,110.00,7475.00\n"
    )


# --- load_fx_rates ---

def _write_fx_csv(path, col, rows):
    """Helper: write a minimal BoC-style FX CSV."""
    path.write_text(f"date,{col}\n" + "".join(f"{d},{r}\n" for d, r in rows))


def test_load_fx_rates_single_file(tmp_path):
    _write_fx_csv(tmp_path / "usd.csv", "FXUSDCAD", [
        ("2025-01-02", "1.4418"),
        ("2025-01-03", "1.4442"),
    ])
    rates = load_fx_rates(tmp_path)
    assert rates["USD"]["2025-01-02"] == "1.4418"
    assert rates["USD"]["2025-01-03"] == "1.4442"


def test_load_fx_rates_merges_two_files_same_currency(tmp_path):
    _write_fx_csv(tmp_path / "usd_2024.csv", "FXUSDCAD", [("2024-12-31", "1.4400")])
    _write_fx_csv(tmp_path / "usd_2025.csv", "FXUSDCAD", [("2025-01-02", "1.4418")])
    rates = load_fx_rates(tmp_path)
    assert "2024-12-31" in rates["USD"]
    assert "2025-01-02" in rates["USD"]


def test_load_fx_rates_invalid_file_raises(tmp_path):
    (tmp_path / "bad.csv").write_text("date,price\n2025-01-02,98.50\n")
    with pytest.raises(ValueError, match="no Bank of Canada FX column"):
        load_fx_rates(tmp_path)


# --- lookup_rate ---

RATE_DATA = {
    "2025-01-02": "1.4418",
    "2025-01-03": "1.4442",
    "2025-01-06": "1.4348",  # Monday (gap: Sat/Sun 4th/5th absent)
}


def test_lookup_rate_exact_match():
    assert lookup_rate(RATE_DATA, "USD", "2025-01-03") == "1.4442"


def test_lookup_rate_falls_back_to_previous_date():
    # 2025-01-04 is Saturday — should fall back to Friday 2025-01-03
    assert lookup_rate(RATE_DATA, "USD", "2025-01-04") == "1.4442"


def test_lookup_rate_date_after_max_raises():
    with pytest.raises(ValueError, match="latest available is 2025-01-06"):
        lookup_rate(RATE_DATA, "USD", "2025-06-01")


def test_lookup_rate_date_before_min_raises():
    with pytest.raises(ValueError, match="earliest available is 2025-01-02"):
        lookup_rate(RATE_DATA, "USD", "2024-12-31")


# --- apply_fx_rates ---

def test_apply_fx_rates_fills_non_cad_row():
    rows = [{"date": "2025-01-03", "currency": "USD", "ticker": "AAPL", "type": "BUY",
              "quantity": "10", "price": "180.00"}]
    fx = {"USD": {"2025-01-03": "1.4442"}}
    out = apply_fx_rates(rows, fx)
    assert out[0]["exchange_rate"] == "1.4442"


def test_apply_fx_rates_skips_cad_row():
    rows = [{"date": "2025-01-03", "currency": "CAD", "ticker": "VFV", "type": "BUY",
              "quantity": "10", "price": "98.50"}]
    out = apply_fx_rates(rows, {})
    assert "exchange_rate" not in out[0]


def test_apply_fx_rates_skips_row_with_existing_rate():
    rows = [{"date": "2025-01-03", "currency": "USD", "ticker": "AAPL", "type": "BUY",
              "quantity": "10", "price": "180.00", "exchange_rate": "1.50"}]
    out = apply_fx_rates(rows, {"USD": {"2025-01-03": "1.4442"}})
    assert out[0]["exchange_rate"] == "1.50"


def test_apply_fx_rates_unknown_currency_raises():
    rows = [{"date": "2025-01-03", "currency": "EUR", "ticker": "X", "type": "BUY",
              "quantity": "1", "price": "10.00"}]
    with pytest.raises(ValueError, match="no FX rate data for 'EUR'"):
        apply_fx_rates(rows, {})


# --- skip_types ---

def test_skip_types_drops_matching_rows():
    config = {**BASE_CONFIG, "skip_types": ["Dividend"]}
    rows = [
        make_row(**{"Txn Type": "BUY"}),
        make_row(**{"Txn Type": "Dividend"}),
        make_row(**{"Txn Type": "BUY"}),
    ]
    out = list(translate_rows(rows, config))
    assert len(out) == 2
    assert all(r["type"] == "BUY" for r in out)


def test_skip_types_does_not_raise_for_skipped_unknown_type():
    config = {**BASE_CONFIG, "type_map": {"BUY": "BUY"}, "skip_types": ["Dividend"]}
    rows = [make_row(**{"Txn Type": "Dividend"})]
    out = list(translate_rows(rows, config))
    assert out == []


def test_skip_types_empty_list_skips_nothing():
    config = {**BASE_CONFIG, "skip_types": []}
    rows = [make_row(**{"Txn Type": "BUY"})]
    out = list(translate_rows(rows, config))
    assert len(out) == 1


# --- negative quantity ---

def test_negative_quantity_stripped():
    rows = [make_row(**{"Shares": "-1.6289"})]
    out = list(translate_rows(rows, BASE_CONFIG))
    assert out[0]["quantity"] == "1.6289"


def test_positive_quantity_unchanged():
    rows = [make_row(**{"Shares": "1.6289"})]
    out = list(translate_rows(rows, BASE_CONFIG))
    assert out[0]["quantity"] == "1.6289"


# --- CLI --fx-dir ---

def test_cli_fx_dir_populates_exchange_rate(tmp_path):
    src = tmp_path / "broker.csv"
    src.write_text(
        "Trade Date,Symbol,Txn Type,Shares,Price ($),Currency\n"
        "2025-01-03,AAPL,BUY,10,180.00,USD\n"
        "2025-01-06,VFV,BUY,100,98.50,CAD\n"
    )
    config = {
        "column_map": {
            "Trade Date": "date",
            "Symbol": "ticker",
            "Txn Type": "type",
            "Shares": "quantity",
            "Price ($)": "price",
            "Currency": "currency",
        }
    }
    cfg = tmp_path / "map.json"
    cfg.write_text(json.dumps(config))
    fx_dir = tmp_path / "fx"
    fx_dir.mkdir()
    _write_fx_csv(fx_dir / "usd.csv", "FXUSDCAD", [
        ("2025-01-02", "1.4418"),
        ("2025-01-03", "1.4442"),
        ("2025-01-06", "1.4348"),
    ])
    out = tmp_path / "out.csv"
    subprocess.run(
        [sys.executable, str(REPO / "translate.py"), str(src), str(cfg),
         "-o", str(out), "--fx-dir", str(fx_dir)],
        check=True,
    )
    assert out.read_text() == (
        "date,ticker,type,quantity,price,currency,exchange_rate\n"
        "2025-01-03,AAPL,BUY,10,180.00,USD,1.4442\n"
        "2025-01-06,VFV,BUY,100,98.50,CAD,\n"
    )


# --- sweep_funds ---

SWEEP_CONFIG = {
    **BASE_CONFIG,
    "sweep_funds": {
        "VMFXX": {
            "quantity_col": "Net Amount",
            "price_override": "1.0",
        }
    },
}


def make_sweep_row(**kwargs):
    return {
        "Trade Date": "2025-01-02",
        "Symbol": "VMFXX",
        "Txn Type": "BUY",
        "Shares": "0",
        "Price ($)": "0",
        "Net Amount": "-53.03",
        **kwargs,
    }


def test_sweep_fund_quantity_taken_from_alternate_col():
    out = list(translate_rows([make_sweep_row()], SWEEP_CONFIG))
    assert out[0]["quantity"] == "53.03"


def test_sweep_fund_price_override_applied():
    out = list(translate_rows([make_sweep_row()], SWEEP_CONFIG))
    assert out[0]["price"] == "1.0"


def test_sweep_fund_reinvestment_price_zero_overridden():
    """Reinvestment rows have Share Price=0; price_override must fix them."""
    out = list(translate_rows([make_sweep_row(**{"Price ($)": "0", "Net Amount": "-0.01"})], SWEEP_CONFIG))
    assert out[0]["price"] == "1.0"
    assert out[0]["quantity"] == "0.01"


def test_sweep_fund_negative_net_amount_sign_stripped():
    out = list(translate_rows([make_sweep_row(**{"Net Amount": "-198.38"})], SWEEP_CONFIG))
    assert out[0]["quantity"] == "198.38"


def test_sweep_fund_positive_net_amount_unchanged():
    out = list(translate_rows([make_sweep_row(**{"Net Amount": "53.32"})], SWEEP_CONFIG))
    assert out[0]["quantity"] == "53.32"


def test_sweep_fund_non_sweep_ticker_unaffected():
    """A regular ticker in the same config must not get the sweep override."""
    row = make_row(**{"Shares": "10", "Price ($)": "290.79"})
    row["Net Amount"] = "-290.79"
    config = {**SWEEP_CONFIG}
    out = list(translate_rows([row], config))
    assert out[0]["quantity"] == "10"
    assert out[0]["price"] == "290.79"


def test_sweep_fund_missing_quantity_col_raises():
    config = {
        **BASE_CONFIG,
        "sweep_funds": {"VMFXX": {"quantity_col": "Nonexistent Column"}},
    }
    with pytest.raises(ValueError, match="quantity_col"):
        list(translate_rows([make_sweep_row()], config))

"""Tests for translate.py."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from translate import clean_number, filter_by_date, load_config, translate_rows, validate_columns

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

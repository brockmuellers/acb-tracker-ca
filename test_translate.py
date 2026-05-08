"""Tests for translate.py."""

import subprocess
import sys
from pathlib import Path

import yaml

import pytest

from translate_lib import (
    AppConfig,
    apply_fx_rates,
    clean_number,
    filter_by_date,
    load_config,
    load_fx_rates,
    lookup_rate,
    translate_file,
    translate_rows,
    validate_column_map,
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
    out = list(translate_rows(rows, AppConfig.from_dict(BASE_CONFIG)))
    assert out[0]["date"] == "2024-01-15"
    assert out[0]["ticker"] == "VFV"
    assert out[0]["type"] == "BUY"
    assert out[0]["quantity"] == "100"
    assert out[0]["price"] == "98.50"


def test_unmapped_columns_dropped():
    rows = [make_row()]
    out = list(translate_rows(rows, AppConfig.from_dict(BASE_CONFIG)))
    assert "Ignored Column" not in out[0]
    assert "Trade Date" not in out[0]


# --- type_map ---

def test_type_map_remaps_value():
    config = AppConfig.from_dict({**BASE_CONFIG, "type_map": {"Buy": "BUY", "Sell": "SELL"}})
    rows = [make_row(**{"Txn Type": "Buy"})]
    out = list(translate_rows(rows, config))
    assert out[0]["type"] == "BUY"


def test_type_map_unknown_value_raises():
    config = AppConfig.from_dict({**BASE_CONFIG, "type_map": {"Buy": "BUY"}})
    rows = [make_row(**{"Txn Type": "Dividend"})]
    with pytest.raises(ValueError, match="unknown type value 'Dividend'"):
        list(translate_rows(rows, config))


def test_no_type_map_passes_value_through():
    rows = [make_row(**{"Txn Type": "BUY"})]
    out = list(translate_rows(rows, AppConfig.from_dict(BASE_CONFIG)))
    assert out[0]["type"] == "BUY"


# --- date_format ---

def test_date_format_converts_to_iso():
    config = AppConfig.from_dict({**BASE_CONFIG, "date_format": "%m/%d/%Y"})
    rows = [make_row(**{"Trade Date": "01/15/2024"})]
    out = list(translate_rows(rows, config))
    assert out[0]["date"] == "2024-01-15"


def test_no_date_format_passes_date_through():
    rows = [make_row(**{"Trade Date": "2024-01-15"})]
    out = list(translate_rows(rows, AppConfig.from_dict(BASE_CONFIG)))
    assert out[0]["date"] == "2024-01-15"


# --- value cleaning ---

def test_dollar_stripped_from_price():
    rows = [make_row(**{"Price ($)": "$98.50"})]
    out = list(translate_rows(rows, AppConfig.from_dict(BASE_CONFIG)))
    assert out[0]["price"] == "98.50"


def test_commas_stripped_from_quantity():
    rows = [make_row(**{"Shares": "1,000"})]
    out = list(translate_rows(rows, AppConfig.from_dict(BASE_CONFIG)))
    assert out[0]["quantity"] == "1000"


# --- defaults ---

def test_defaults_fill_missing_column():
    config = AppConfig.from_dict({**BASE_CONFIG, "defaults": {"currency": "CAD"}})
    rows = [make_row()]
    out = list(translate_rows(rows, config))
    assert out[0]["currency"] == "CAD"


def test_defaults_fill_empty_column():
    config = AppConfig.from_dict({
        "column_map": {**BASE_CONFIG["column_map"], "Currency": "currency"},
        "defaults": {"currency": "CAD"},
    })
    rows = [make_row(**{"Currency": ""})]
    out = list(translate_rows(rows, config))
    assert out[0]["currency"] == "CAD"


def test_defaults_do_not_overwrite_existing_value():
    config = AppConfig.from_dict({
        "column_map": {**BASE_CONFIG["column_map"], "Currency": "currency"},
        "defaults": {"currency": "CAD"},
    })
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
    cfg = tmp_path / "map.yaml"
    cfg.write_text(yaml.dump(BASE_CONFIG))
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
    cfg = tmp_path / "map.yaml"
    cfg.write_text(yaml.dump(BASE_CONFIG))
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
    cfg = tmp_path / "map.yaml"
    cfg.write_text(yaml.dump(BASE_CONFIG))
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
    config = AppConfig.from_dict({
        "column_map": {
            "Trade Date": "date",
            "Symbol": "ticker",
            # "type" intentionally missing
            "Shares": "quantity",
            "Price ($)": "price",
        }
    })
    rows = [make_row()]
    with pytest.raises(ValueError, match="missing required column 'type'"):
        validate_columns(translate_rows(rows, config))


def test_validate_passes_with_all_required_columns():
    rows = [make_row()]
    result = validate_columns(translate_rows(rows, AppConfig.from_dict(BASE_CONFIG)))
    assert len(result) == 1


def test_validate_empty_input_returns_empty():
    result = validate_columns(translate_rows([], AppConfig.from_dict(BASE_CONFIG)))
    assert result == []


# --- load_config ---

def test_load_config_raises_without_column_map(tmp_path):
    cfg = tmp_path / "bad.yaml"
    cfg.write_text(yaml.dump({"type_map": {"Buy": "BUY"}}))
    with pytest.raises(ValueError, match="column_map"):
        load_config(str(cfg))


def test_load_config_raises_with_empty_column_map(tmp_path):
    cfg = tmp_path / "bad.yaml"
    cfg.write_text(yaml.dump({"column_map": {}}))
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
    cfg = tmp_path / "map.yaml"
    cfg.write_text(yaml.dump(BASE_CONFIG))
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
    cfg = tmp_path / "map.yaml"
    cfg.write_text(yaml.dump(config))
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
         str(src), str(REPO / "mappings" / "wealthsimple.yaml"),
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
    cfg = tmp_path / "map.yaml"
    cfg.write_text(yaml.dump(BASE_CONFIG))
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
        "account_number,date,ticker,type,quantity,price,currency,exchange_rate,amount_cad,acb_cad,gain_loss_cad,superficial_loss_cad\n"
        ",2024-01-15,VFV,BUY,100,98.50,CAD,1,9850.00,9850.00,,\n"
        ",2024-03-10,VFV,BUY,50,102.00,CAD,1,5100.00,14950.00,,\n"
        ",2024-06-20,VFV,SELL,75,110.00,CAD,1,8250.00,7475.00,775.00,\n"
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
    config = AppConfig.from_dict({**BASE_CONFIG, "skip_types": ["Dividend"]})
    rows = [
        make_row(**{"Txn Type": "BUY"}),
        make_row(**{"Txn Type": "Dividend"}),
        make_row(**{"Txn Type": "BUY"}),
    ]
    out = list(translate_rows(rows, config))
    assert len(out) == 2
    assert all(r["type"] == "BUY" for r in out)


def test_skip_types_does_not_raise_for_skipped_unknown_type():
    config = AppConfig.from_dict({**BASE_CONFIG, "type_map": {"BUY": "BUY"}, "skip_types": ["Dividend"]})
    rows = [make_row(**{"Txn Type": "Dividend"})]
    out = list(translate_rows(rows, config))
    assert out == []


def test_skip_types_empty_list_skips_nothing():
    config = AppConfig.from_dict({**BASE_CONFIG, "skip_types": []})
    rows = [make_row(**{"Txn Type": "BUY"})]
    out = list(translate_rows(rows, config))
    assert len(out) == 1


# --- zero quantity/price validation ---

def test_zero_quantity_raises():
    rows = [make_row(**{"Shares": "0"})]
    with pytest.raises(ValueError, match="quantity is 0"):
        list(translate_rows(rows, AppConfig.from_dict(BASE_CONFIG)))


def test_zero_price_raises():
    rows = [make_row(**{"Price ($)": "0"})]
    with pytest.raises(ValueError, match="price is 0"):
        list(translate_rows(rows, AppConfig.from_dict(BASE_CONFIG)))


def test_nonzero_quantity_and_price_do_not_raise():
    rows = [make_row()]
    out = list(translate_rows(rows, AppConfig.from_dict(BASE_CONFIG)))
    assert len(out) == 1


def test_zero_validation_fires_after_sweep_override():
    """A sweep row with price_override must not raise even if mapped price column is 0."""
    config = AppConfig.from_dict({
        **BASE_CONFIG,
        "sweep_types": {
            "types": ["Sweep in"],
            "quantity_col": "Net Amount",
            "price_override": "1.0",
        },
    })
    row = make_sweep_row()  # Price ($)=0, Net Amount=-53.03
    out = list(translate_rows([row], config))
    assert out[0]["price"] == "1.0"
    assert out[0]["quantity"] == "53.03"


# --- negative quantity ---

def test_negative_quantity_stripped():
    rows = [make_row(**{"Shares": "-1.6289"})]
    out = list(translate_rows(rows, AppConfig.from_dict(BASE_CONFIG)))
    assert out[0]["quantity"] == "1.6289"


def test_positive_quantity_unchanged():
    rows = [make_row(**{"Shares": "1.6289"})]
    out = list(translate_rows(rows, AppConfig.from_dict(BASE_CONFIG)))
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
    cfg = tmp_path / "map.yaml"
    cfg.write_text(yaml.dump(config))
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


# --- sweep_types ---

SWEEP_CONFIG = AppConfig.from_dict({
    **BASE_CONFIG,
    "sweep_types": {
        "types": ["Sweep in", "Sweep out"],
        "quantity_col": "Net Amount",
        "price_override": "1.0",
    },
})


def make_sweep_row(**kwargs):
    return {
        "Trade Date": "2025-01-02",
        "Symbol": "VMFXX",
        "Txn Type": "Sweep in",
        "Shares": "0",
        "Price ($)": "0",
        "Net Amount": "-53.03",
        **kwargs,
    }


def test_sweep_type_quantity_taken_from_alternate_col():
    out = list(translate_rows([make_sweep_row()], SWEEP_CONFIG))
    assert out[0]["quantity"] == "53.03"


def test_sweep_type_price_override_applied():
    out = list(translate_rows([make_sweep_row()], SWEEP_CONFIG))
    assert out[0]["price"] == "1.0"


def test_sweep_type_price_zero_overridden():
    """Rows with Price=0 get corrected by price_override."""
    out = list(translate_rows([make_sweep_row(**{"Price ($)": "0", "Net Amount": "-0.01"})], SWEEP_CONFIG))
    assert out[0]["price"] == "1.0"
    assert out[0]["quantity"] == "0.01"


def test_sweep_type_negative_net_amount_sign_stripped():
    out = list(translate_rows([make_sweep_row(**{"Net Amount": "-198.38"})], SWEEP_CONFIG))
    assert out[0]["quantity"] == "198.38"


def test_sweep_type_positive_net_amount_unchanged():
    out = list(translate_rows([make_sweep_row(**{"Txn Type": "Sweep out", "Net Amount": "53.32"})], SWEEP_CONFIG))
    assert out[0]["quantity"] == "53.32"


def test_sweep_type_non_sweep_transaction_unaffected():
    """A regular BUY row must not get the sweep override."""
    row = make_row(**{"Shares": "10", "Price ($)": "290.79"})
    row["Net Amount"] = "-290.79"
    out = list(translate_rows([row], SWEEP_CONFIG))
    assert out[0]["quantity"] == "10"
    assert out[0]["price"] == "290.79"


def test_sweep_type_missing_quantity_col_raises():
    config = AppConfig.from_dict({
        **BASE_CONFIG,
        "sweep_types": {"types": ["Sweep in"], "quantity_col": "Nonexistent Column"},
    })
    with pytest.raises(ValueError, match="quantity_col"):
        list(translate_rows([make_sweep_row()], config))


# --- validate_column_map ---

CSV_HEADERS = ["Trade Date", "Symbol", "Txn Type", "Shares", "Price ($)", "Net Amount"]


def test_validate_column_map_passes_when_all_present():
    validate_column_map(BASE_CONFIG["column_map"], CSV_HEADERS)


def test_validate_column_map_raises_for_missing_column():
    column_map = {**BASE_CONFIG["column_map"], "Nonexistent Col": "currency"}
    with pytest.raises(ValueError, match="Nonexistent Col"):
        validate_column_map(column_map, CSV_HEADERS, config_path="test.json")


def test_validate_column_map_error_lists_available_columns():
    column_map = {**BASE_CONFIG["column_map"], "Typo Col": "currency"}
    with pytest.raises(ValueError, match="Available columns"):
        validate_column_map(column_map, CSV_HEADERS, config_path="test.json")


def test_validate_column_map_raises_for_missing_sweep_quantity_col():
    with pytest.raises(ValueError, match="Missing Col.*sweep_types"):
        validate_column_map(
            BASE_CONFIG["column_map"],
            CSV_HEADERS,
            config_path="test.json",
            sweep_quantity_col="Missing Col",
        )


def test_validate_column_map_passes_when_sweep_quantity_col_present():
    validate_column_map(
        BASE_CONFIG["column_map"],
        CSV_HEADERS,
        sweep_quantity_col="Net Amount",
    )


def test_validate_column_map_optional_column_absent_no_error():
    column_map = {**BASE_CONFIG["column_map"], "Time": "time"}
    validate_column_map(column_map, CSV_HEADERS, optional_columns={"Time"})


def test_validate_column_map_optional_column_absent_still_raises_for_required():
    column_map = {**BASE_CONFIG["column_map"], "Time": "time", "Missing": "currency"}
    with pytest.raises(ValueError, match="Missing"):
        validate_column_map(column_map, CSV_HEADERS, optional_columns={"Time"})


def test_appconfig_optional_columns_entry_not_in_column_map_raises():
    with pytest.raises(ValueError, match="not found in 'column_map'"):
        AppConfig.from_dict({**BASE_CONFIG, "optional_columns": ["NotAKey"]})


def test_appconfig_optional_columns_not_list_raises():
    with pytest.raises(ValueError, match="must be a list"):
        AppConfig.from_dict({**BASE_CONFIG, "optional_columns": "Time"})


def test_translate_file_optional_column_absent_silently_omitted(tmp_path):
    csv_path = tmp_path / "in.csv"
    csv_path.write_text("Trade Date,Symbol,Txn Type,Shares,Price ($)\n2024-01-15,VFV,BUY,100,98.50\n")
    mapping_path = tmp_path / "map.yaml"
    mapping_path.write_text(yaml.dump({
        **BASE_CONFIG,
        "column_map": {**BASE_CONFIG["column_map"], "Time": "time"},
        "optional_columns": ["Time"],
    }))
    rows = translate_file(str(csv_path), str(mapping_path))
    assert len(rows) == 1
    assert "time" not in rows[0]


def test_translate_file_optional_column_present_flows_through(tmp_path):
    csv_path = tmp_path / "in.csv"
    csv_path.write_text("Trade Date,Symbol,Txn Type,Shares,Price ($),Time\n2024-01-15,VFV,BUY,100,98.50,10:30\n")
    mapping_path = tmp_path / "map.yaml"
    mapping_path.write_text(yaml.dump({
        **BASE_CONFIG,
        "column_map": {**BASE_CONFIG["column_map"], "Time": "time"},
        "optional_columns": ["Time"],
    }))
    rows = translate_file(str(csv_path), str(mapping_path))
    assert rows[0]["time"] == "10:30"


# --- improved error context (row numbers, column names) ---

def test_unknown_type_error_includes_row_number():
    config = AppConfig.from_dict({**BASE_CONFIG, "type_map": {"Buy": "BUY", "Sell": "SELL"}})
    rows = [make_row(**{"Txn Type": "Buy"}), make_row(**{"Txn Type": "Dividend"})]
    with pytest.raises(ValueError, match=r"row 3"):
        list(translate_rows(rows, config))


def test_date_parse_error_includes_row_number_and_value():
    config = AppConfig.from_dict({**BASE_CONFIG, "date_format": "%m/%d/%Y"})
    rows = [make_row(**{"Trade Date": "2024-01-15"})]
    with pytest.raises(ValueError, match=r"row 2.*2024-01-15.*%m/%d/%Y"):
        list(translate_rows(rows, config))


def test_date_parse_error_includes_ticker():
    config = AppConfig.from_dict({**BASE_CONFIG, "date_format": "%m/%d/%Y"})
    rows = [make_row(**{"Trade Date": "2024-01-15", "Symbol": "AAPL"})]
    with pytest.raises(ValueError, match="AAPL"):
        list(translate_rows(rows, config))


def test_blank_type_raises_clear_error():
    rows = [make_row(**{"Txn Type": ""})]
    with pytest.raises(ValueError, match="row 2.*'type' column is blank"):
        list(translate_rows(rows, AppConfig.from_dict(BASE_CONFIG)))


def test_zero_quantity_error_includes_row_number():
    rows = [make_row(**{"Shares": "0"})]
    with pytest.raises(ValueError, match=r"row 2"):
        list(translate_rows(rows, AppConfig.from_dict(BASE_CONFIG)))


# --- AppConfig.from_dict ---

MINIMAL_CONFIG = {"column_map": {"Date": "date", "Symbol": "ticker", "Action": "type", "Qty": "quantity", "Price": "price"}}
FULL_CONFIG = {
    **MINIMAL_CONFIG,
    "type_map": {"Buy": "BUY", "Sell": "SELL"},
    "skip_types": ["Dividend"],
    "date_format": "%m/%d/%Y",
    "defaults": {"currency": "CAD"},
    "sweep_types": {"types": ["Sweep in"], "quantity_col": "Net Amount", "price_override": "1.0"},
}


def test_from_dict_passes_minimal():
    result = AppConfig.from_dict(MINIMAL_CONFIG)
    assert result.column_map == MINIMAL_CONFIG["column_map"]


def test_from_dict_passes_full():
    result = AppConfig.from_dict(FULL_CONFIG)
    assert result.type_map == {"Buy": "BUY", "Sell": "SELL"}


def test_from_dict_unknown_top_level_key_raises():
    bad = {**MINIMAL_CONFIG, "colum_map": {}}  # typo
    with pytest.raises(ValueError, match="unknown key"):
        AppConfig.from_dict(bad, "test.json")


def test_from_dict_unknown_key_lists_valid_keys():
    bad = {**MINIMAL_CONFIG, "sweepTypes": {}}  # camelCase typo
    with pytest.raises(ValueError, match="Valid keys"):
        AppConfig.from_dict(bad, "test.json")


def test_from_dict_missing_column_map_raises():
    with pytest.raises(ValueError, match="column_map"):
        AppConfig.from_dict({}, "test.json")


def test_from_dict_empty_column_map_raises():
    with pytest.raises(ValueError, match="column_map"):
        AppConfig.from_dict({"column_map": {}}, "test.json")


def test_from_dict_wrong_type_column_map_raises():
    with pytest.raises(ValueError, match="column_map"):
        AppConfig.from_dict({"column_map": ["list", "not", "dict"]}, "test.json")


def test_from_dict_wrong_type_type_map_raises():
    bad = {**MINIMAL_CONFIG, "type_map": ["Buy", "Sell"]}
    with pytest.raises(ValueError, match="type_map"):
        AppConfig.from_dict(bad, "test.json")


def test_from_dict_wrong_type_skip_types_raises():
    bad = {**MINIMAL_CONFIG, "skip_types": "Dividend"}
    with pytest.raises(ValueError, match="skip_types"):
        AppConfig.from_dict(bad, "test.json")


def test_from_dict_wrong_type_date_format_raises():
    bad = {**MINIMAL_CONFIG, "date_format": 20240101}
    with pytest.raises(ValueError, match="date_format"):
        AppConfig.from_dict(bad, "test.json")


def test_from_dict_wrong_type_defaults_raises():
    bad = {**MINIMAL_CONFIG, "defaults": "CAD"}
    with pytest.raises(ValueError, match="defaults"):
        AppConfig.from_dict(bad, "test.json")


def test_from_dict_sweep_types_missing_types_raises():
    bad = {**MINIMAL_CONFIG, "sweep_types": {"quantity_col": "Net Amount"}}
    with pytest.raises(ValueError, match="sweep_types.'types'"):
        AppConfig.from_dict(bad, "test.json")


def test_from_dict_sweep_types_empty_types_raises():
    bad = {**MINIMAL_CONFIG, "sweep_types": {"types": []}}
    with pytest.raises(ValueError, match="sweep_types.'types'"):
        AppConfig.from_dict(bad, "test.json")


def test_from_dict_sweep_types_unknown_key_raises():
    bad = {**MINIMAL_CONFIG, "sweep_types": {"types": ["Sweep in"], "qty_col": "Net Amount"}}  # typo
    with pytest.raises(ValueError, match="sweep_types has unknown key"):
        AppConfig.from_dict(bad, "test.json")


def test_from_dict_sweep_types_unknown_key_lists_valid_keys():
    bad = {**MINIMAL_CONFIG, "sweep_types": {"types": ["Sweep in"], "qty_col": "Net Amount"}}
    with pytest.raises(ValueError, match="Valid sweep_types keys"):
        AppConfig.from_dict(bad, "test.json")


def test_from_dict_error_includes_config_path():
    with pytest.raises(ValueError, match="mybroker.json"):
        AppConfig.from_dict({}, "mybroker.json")

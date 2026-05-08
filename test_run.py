"""Tests for run.py — the master pipeline orchestrator."""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from run import _expand_input, _resolve, load_run_config
from translate_lib import ConfigurationError

REPO = Path(__file__).parent


def write_config(path, data):
    path.write_text(yaml.dump(data))
    return path


def write_csv(path, content):
    path.write_text(textwrap.dedent(content))
    return path


def simple_mapping(tmp_path):
    m = tmp_path / "mapping.yaml"
    m.write_text(yaml.dump({
        "column_map": {
            "date": "date",
            "ticker": "ticker",
            "type": "type",
            "quantity": "quantity",
            "price": "price",
        }
    }))
    return m


def simple_csv(tmp_path, name="trades.csv"):
    f = tmp_path / name
    f.write_text(
        "date,ticker,type,quantity,price\n"
        "2024-01-15,VFV,BUY,100,98.50\n"
        "2024-03-10,VFV,BUY,50,102.00\n"
        "2024-06-20,VFV,SELL,75,110.00\n"
    )
    return f


# --- load_run_config ---

def test_load_run_config_minimal(tmp_path):
    cfg = write_config(tmp_path / "run.yaml", {
        "sources": [{"input": "trades.csv", "mapping": "map.yaml"}]
    })
    data = load_run_config(cfg)
    assert len(data["sources"]) == 1


def test_load_run_config_unknown_top_key(tmp_path):
    cfg = write_config(tmp_path / "run.yaml", {
        "sources": [{"input": "a.csv", "mapping": "m.yaml"}],
        "extra_key": "bad",
    })
    with pytest.raises(ConfigurationError, match="unknown key"):
        load_run_config(cfg)


def test_load_run_config_unknown_source_key(tmp_path):
    cfg = write_config(tmp_path / "run.yaml", {
        "sources": [{"input": "a.csv", "mapping": "m.yaml", "bogus": True}]
    })
    with pytest.raises(ConfigurationError, match="unknown key"):
        load_run_config(cfg)


def test_load_run_config_missing_input_key(tmp_path):
    cfg = write_config(tmp_path / "run.yaml", {
        "sources": [{"mapping": "m.yaml"}]
    })
    with pytest.raises(ConfigurationError, match="missing required key 'input'"):
        load_run_config(cfg)


def test_load_run_config_missing_mapping_key(tmp_path):
    cfg = write_config(tmp_path / "run.yaml", {
        "sources": [{"input": "a.csv"}]
    })
    with pytest.raises(ConfigurationError, match="missing required key 'mapping'"):
        load_run_config(cfg)


def test_load_run_config_empty_sources_raises(tmp_path):
    cfg = write_config(tmp_path / "run.yaml", {"sources": []})
    with pytest.raises(ConfigurationError, match="non-empty list"):
        load_run_config(cfg)


def test_load_run_config_sources_not_list(tmp_path):
    cfg = write_config(tmp_path / "run.yaml", {"sources": "bad"})
    with pytest.raises(ConfigurationError, match="non-empty list"):
        load_run_config(cfg)


# --- _resolve ---

def test_resolve_relative_to_base(tmp_path):
    result = _resolve(tmp_path, "sub/file.csv")
    assert result == tmp_path / "sub" / "file.csv"


def test_resolve_absolute_unchanged(tmp_path):
    abs_path = "/absolute/path/file.csv"
    result = _resolve(tmp_path, abs_path)
    assert str(result) == abs_path


# --- _expand_input ---

def test_expand_input_single_file(tmp_path):
    f = tmp_path / "a.csv"
    f.touch()
    result = _expand_input(tmp_path, "a.csv")
    assert result == [f]


def test_expand_input_glob(tmp_path):
    sub = tmp_path / "exports"
    sub.mkdir()
    (sub / "b.csv").touch()
    (sub / "a.csv").touch()
    result = _expand_input(tmp_path, "exports/*.csv")
    assert result == [sub / "a.csv", sub / "b.csv"]


def test_expand_input_no_match_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="no files matched"):
        _expand_input(tmp_path, "nonexistent/*.csv")


# --- end-to-end CLI tests ---

def test_cli_single_source_stdout(tmp_path):
    simple_csv(tmp_path)
    simple_mapping(tmp_path)
    write_config(tmp_path / "run.yaml", {
        "sources": [{"input": "trades.csv", "mapping": "mapping.yaml"}]
    })
    result = subprocess.run(
        [sys.executable, str(REPO / "run.py"), str(tmp_path / "run.yaml")],
        capture_output=True, text=True, check=True,
    )
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "account_number,date,ticker,type,quantity,price,currency,exchange_rate,amount_cad,acb_cad,gain_loss_cad,superficial_loss_cad"
    assert len(lines) == 4  # header + 3 rows


def test_cli_output_to_file(tmp_path):
    simple_csv(tmp_path)
    simple_mapping(tmp_path)
    out = tmp_path / "out.csv"
    write_config(tmp_path / "run.yaml", {
        "sources": [{"input": "trades.csv", "mapping": "mapping.yaml"}],
        "output": "out.csv",
    })
    subprocess.run(
        [sys.executable, str(REPO / "run.py"), str(tmp_path / "run.yaml")],
        check=True,
    )
    assert out.exists()
    lines = out.read_text().splitlines()
    assert len(lines) == 4


def test_cli_output_flag_overrides_config(tmp_path):
    simple_csv(tmp_path)
    simple_mapping(tmp_path)
    config_out = tmp_path / "config_out.csv"
    cli_out = tmp_path / "cli_out.csv"
    write_config(tmp_path / "run.yaml", {
        "sources": [{"input": "trades.csv", "mapping": "mapping.yaml"}],
        "output": str(config_out),
    })
    subprocess.run(
        [sys.executable, str(REPO / "run.py"), str(tmp_path / "run.yaml"),
         "-o", str(cli_out)],
        check=True,
    )
    assert cli_out.exists()
    assert not config_out.exists()


def test_cli_multiple_sources_combined(tmp_path):
    # Two separate CSVs with different tickers; both end up in output.
    vfv = tmp_path / "vfv.csv"
    vfv.write_text(
        "date,ticker,type,quantity,price\n"
        "2024-01-15,VFV,BUY,100,98.50\n"
    )
    xeqt = tmp_path / "xeqt.csv"
    xeqt.write_text(
        "date,ticker,type,quantity,price\n"
        "2024-02-01,XEQT,BUY,200,28.00\n"
    )
    simple_mapping(tmp_path)
    out = tmp_path / "out.csv"
    write_config(tmp_path / "run.yaml", {
        "sources": [
            {"input": "vfv.csv", "mapping": "mapping.yaml"},
            {"input": "xeqt.csv", "mapping": "mapping.yaml"},
        ],
        "output": "out.csv",
    })
    subprocess.run(
        [sys.executable, str(REPO / "run.py"), str(tmp_path / "run.yaml")],
        check=True,
    )
    content = out.read_text()
    assert "VFV" in content
    assert "XEQT" in content


def test_cli_date_filter_start(tmp_path):
    f = tmp_path / "trades.csv"
    f.write_text(
        "date,ticker,type,quantity,price\n"
        "2024-01-15,VFV,BUY,100,98.50\n"
        "2024-03-10,VFV,BUY,50,102.00\n"
        "2024-06-20,VFV,BUY,25,110.00\n"
    )
    simple_mapping(tmp_path)
    out = tmp_path / "out.csv"
    write_config(tmp_path / "run.yaml", {
        "sources": [{"input": "trades.csv", "mapping": "mapping.yaml"}],
        "output": "out.csv",
        "start": "2024-03-01",
    })
    subprocess.run(
        [sys.executable, str(REPO / "run.py"), str(tmp_path / "run.yaml")],
        check=True,
    )
    content = out.read_text()
    lines = content.splitlines()
    assert len(lines) == 3  # header + 2 rows (March and June only)
    assert "2024-01-15" not in content


def test_cli_date_filter_end(tmp_path):
    simple_csv(tmp_path)
    simple_mapping(tmp_path)
    out = tmp_path / "out.csv"
    write_config(tmp_path / "run.yaml", {
        "sources": [{"input": "trades.csv", "mapping": "mapping.yaml"}],
        "output": "out.csv",
        "end": "2024-03-31",
    })
    subprocess.run(
        [sys.executable, str(REPO / "run.py"), str(tmp_path / "run.yaml")],
        check=True,
    )
    content = out.read_text()
    assert "2024-06-20" not in content
    assert "2024-01-15" in content
    assert "2024-03-10" in content


def test_cli_glob_input(tmp_path):
    exports = tmp_path / "exports"
    exports.mkdir()
    for name, ticker in [("a.csv", "VFV"), ("b.csv", "XEQT")]:
        (exports / name).write_text(
            f"date,ticker,type,quantity,price\n"
            f"2024-01-15,{ticker},BUY,100,50.00\n"
        )
    simple_mapping(tmp_path)
    out = tmp_path / "out.csv"
    write_config(tmp_path / "run.yaml", {
        "sources": [{"input": "exports/*.csv", "mapping": "mapping.yaml"}],
        "output": "out.csv",
    })
    subprocess.run(
        [sys.executable, str(REPO / "run.py"), str(tmp_path / "run.yaml")],
        check=True,
    )
    content = out.read_text()
    assert "VFV" in content
    assert "XEQT" in content


def test_cli_missing_input_file_exits_nonzero(tmp_path):
    simple_mapping(tmp_path)
    write_config(tmp_path / "run.yaml", {
        "sources": [{"input": "nonexistent.csv", "mapping": "mapping.yaml"}]
    })
    result = subprocess.run(
        [sys.executable, str(REPO / "run.py"), str(tmp_path / "run.yaml")],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "nonexistent" in result.stderr


def test_cli_bad_config_exits_nonzero(tmp_path):
    cfg = tmp_path / "run.yaml"
    cfg.write_text("not: valid: config: for: run\nextra_key: bad\n")
    result = subprocess.run(
        [sys.executable, str(REPO / "run.py"), str(cfg)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0


def test_cli_pretty_print(tmp_path):
    simple_csv(tmp_path)
    simple_mapping(tmp_path)
    write_config(tmp_path / "run.yaml", {
        "sources": [{"input": "trades.csv", "mapping": "mapping.yaml"}]
    })
    result = subprocess.run(
        [sys.executable, str(REPO / "run.py"), str(tmp_path / "run.yaml"), "-p"],
        capture_output=True, text=True, check=True,
    )
    # tabulate grid format uses + and | characters
    assert "+" in result.stdout or "|" in result.stdout


def test_cli_values_match_acb_calculation(tmp_path):
    # Verify end-to-end numbers match the known-good hand calculation from test_acb.py:
    # BUY 100 @ 98.50 → acb 9850.00
    # BUY 50 @ 102.00 → acb 14950.00
    # SELL 75 @ 110.00 → gain 775.00, acb 7475.00
    simple_csv(tmp_path)
    simple_mapping(tmp_path)
    out = tmp_path / "out.csv"
    write_config(tmp_path / "run.yaml", {
        "sources": [{"input": "trades.csv", "mapping": "mapping.yaml"}],
        "output": "out.csv",
    })
    subprocess.run(
        [sys.executable, str(REPO / "run.py"), str(tmp_path / "run.yaml")],
        check=True,
    )
    content = out.read_text()
    assert "9850.00" in content
    assert "14950.00" in content
    assert "7475.00" in content
    assert "775.00" in content


def test_cli_output_holdings_file(tmp_path):
    # Two sources with different account numbers: same ticker VFV.
    # A1 buys 100 @ 98.50; A2 buys 50 @ 102.00. ACB = 14950.00.
    csv_a1 = tmp_path / "a1.csv"
    csv_a1.write_text(
        "date,ticker,type,quantity,price\n"
        "2024-01-15,VFV,BUY,100,98.50\n"
    )
    csv_a2 = tmp_path / "a2.csv"
    csv_a2.write_text(
        "date,ticker,type,quantity,price\n"
        "2024-03-10,VFV,BUY,50,102.00\n"
    )
    simple_mapping(tmp_path)
    out = tmp_path / "out.csv"
    holdings_out = tmp_path / "holdings.csv"
    write_config(tmp_path / "run.yaml", {
        "sources": [
            {"input": "a1.csv", "mapping": "mapping.yaml", "account_number": "A1"},
            {"input": "a2.csv", "mapping": "mapping.yaml", "account_number": "A2"},
        ],
        "output": "out.csv",
        "output_holdings": "holdings.csv",
    })
    subprocess.run(
        [sys.executable, str(REPO / "run.py"), str(tmp_path / "run.yaml")],
        check=True,
    )
    assert holdings_out.exists()
    lines = holdings_out.read_text().splitlines()
    assert lines[0] == "account_number,ticker,quantity,acb_cad"
    # A1 row, A2 row, TOTAL row
    assert len(lines) == 4
    assert lines[1].startswith("A1,VFV,100,")
    assert lines[2].startswith("A2,VFV,50,")
    assert lines[3] == "TOTAL,VFV,150,14950.00"

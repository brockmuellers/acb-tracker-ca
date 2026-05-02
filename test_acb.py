"""Tests for acb.py covering the cases verified manually in plan review."""

import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from acb import compute_acb, load_transactions

REPO = Path(__file__).parent


def tx(date, ticker, tx_type, quantity, price):
    """Build a normalized transaction dict, as load_transactions would."""
    return {
        "date": date,
        "ticker": ticker,
        "type": tx_type,
        "quantity": Decimal(str(quantity)),
        "price": Decimal(str(price)),
    }


def acbs(rows):
    """Return just the running acb column from compute_acb output."""
    return [r["acb"] for r in compute_acb(rows)]


def test_single_ticker_matches_hand_calculation():
    # BUY 100 @ 98.50 → 9850
    # BUY  50 @ 102.00 → 14950 (per-share 99.6667)
    # SELL 75 @ 110.00 → ACB removed = 75 * 99.6667 = 7475 → remaining 7475
    rows = [
        tx("2024-01-15", "VFV", "BUY", 100, "98.50"),
        tx("2024-03-10", "VFV", "BUY", 50, "102.00"),
        tx("2024-06-20", "VFV", "SELL", 75, "110.00"),
    ]
    assert acbs(rows) == [Decimal("9850.00"), Decimal("14950.00"), Decimal("7475.00")]


def test_multi_ticker_independence():
    # VFV math should match the single-ticker case despite XEQT interleaving.
    # XEQT: 200 @ 28 → 5600; +100 @ 30 → 8600 (avg 28.6667);
    #       SELL 150 → ACB removed 150 * 28.6667 = 4300 → remaining 4300
    rows = [
        tx("2024-01-15", "VFV", "BUY", 100, "98.50"),
        tx("2024-02-01", "XEQT", "BUY", 200, "28.00"),
        tx("2024-03-10", "VFV", "BUY", 50, "102.00"),
        tx("2024-04-05", "XEQT", "BUY", 100, "30.00"),
        tx("2024-06-20", "VFV", "SELL", 75, "110.00"),
        tx("2024-07-01", "XEQT", "SELL", 150, "32.00"),
    ]
    out = list(compute_acb(rows))
    vfv = [r["acb"] for r in out if r["ticker"] == "VFV"]
    xeqt = [r["acb"] for r in out if r["ticker"] == "XEQT"]
    assert vfv == [Decimal("9850.00"), Decimal("14950.00"), Decimal("7475.00")]
    assert xeqt == [Decimal("5600.00"), Decimal("8600.00"), Decimal("4300.00")]


def test_decimal_precision_lands_exactly_at_zero():
    # Three identical buys then three identical sells should land on 0.00
    # exactly — no float drift.
    rows = [
        tx("2024-01-01", "FOO", "BUY", 1, 10),
        tx("2024-01-02", "FOO", "BUY", 1, 10),
        tx("2024-01-03", "FOO", "BUY", 1, 10),
        tx("2024-01-04", "FOO", "SELL", 1, 15),
        tx("2024-01-05", "FOO", "SELL", 1, 15),
        tx("2024-01-06", "FOO", "SELL", 1, 15),
    ]
    assert acbs(rows)[-1] == Decimal("0.00")


def test_oversell_raises_clear_error():
    rows = [
        tx("2024-01-01", "FOO", "BUY", 10, 5),
        tx("2024-02-01", "FOO", "SELL", 11, 6),
    ]
    with pytest.raises(ValueError, match="SELL of FOO on 2024-02-01 exceeds holdings"):
        list(compute_acb(rows))


def test_sell_without_prior_buy_raises():
    rows = [tx("2024-01-01", "FOO", "SELL", 1, 5)]
    with pytest.raises(ValueError, match="exceeds holdings"):
        list(compute_acb(rows))


def test_unknown_transaction_type_raises():
    rows = [tx("2024-01-01", "FOO", "DRIP", 1, 5)]
    with pytest.raises(ValueError, match="Unknown transaction type"):
        list(compute_acb(rows))


def test_input_is_reordered_chronologically():
    # Out-of-order input should be processed in date order.
    rows = [
        tx("2024-06-20", "VFV", "SELL", 75, "110.00"),
        tx("2024-01-15", "VFV", "BUY", 100, "98.50"),
        tx("2024-03-10", "VFV", "BUY", 50, "102.00"),
    ]
    out = list(compute_acb(rows))
    assert [r["date"] for r in out] == ["2024-01-15", "2024-03-10", "2024-06-20"]
    assert [r["acb"] for r in out] == [
        Decimal("9850.00"),
        Decimal("14950.00"),
        Decimal("7475.00"),
    ]


def test_cli_end_to_end_on_sample_fixture(tmp_path):
    out_path = tmp_path / "out.csv"
    subprocess.run(
        [sys.executable, str(REPO / "acb.py"),
         str(REPO / "sample_transactions.csv"), "-o", str(out_path)],
        check=True,
    )
    # read_text() normalizes \r\n → \n; that's fine for content equality.
    assert out_path.read_text() == (
        "date,ticker,type,quantity,price,acb\n"
        "2024-01-15,VFV,BUY,100,98.50,9850.00\n"
        "2024-03-10,VFV,BUY,50,102.00,14950.00\n"
        "2024-06-20,VFV,SELL,75,110.00,7475.00\n"
    )


def test_start_establishes_opening_balance():
    # START 100 @ 95.00 → opening total ACB = 9500
    rows = [tx("2023-12-31", "VFV", "START", 100, "95.00")]
    assert acbs(rows) == [Decimal("9500.00")]


def test_start_then_buy_averages_correctly():
    # START 100 @ 95 → 9500, BUY 50 @ 102 → 9500+5100 = 14600
    # per-share = 14600/150 = 97.3333...
    rows = [
        tx("2023-12-31", "VFV", "START", 100, "95.00"),
        tx("2024-03-10", "VFV", "BUY", 50, "102.00"),
    ]
    assert acbs(rows) == [Decimal("9500.00"), Decimal("14600.00")]


def test_start_then_sell_uses_opening_acb():
    # START 100 @ 95 → 9500, SELL 40 @ 120 → ACB removed = 40*95 = 3800
    # → remaining 5700; per-share still 95.
    rows = [
        tx("2023-12-31", "VFV", "START", 100, "95.00"),
        tx("2024-06-20", "VFV", "SELL", 40, "120.00"),
    ]
    assert acbs(rows) == [Decimal("9500.00"), Decimal("5700.00")]


def test_start_after_buy_for_same_ticker_raises():
    rows = [
        tx("2024-01-15", "VFV", "BUY", 100, "98.50"),
        tx("2024-06-20", "VFV", "START", 50, "95.00"),
    ]
    with pytest.raises(ValueError, match="START for VFV on 2024-06-20 must precede"):
        list(compute_acb(rows))


def test_two_starts_for_same_ticker_raises():
    rows = [
        tx("2023-12-31", "VFV", "START", 100, "95.00"),
        tx("2024-01-01", "VFV", "START", 50, "100.00"),
    ]
    with pytest.raises(ValueError, match="START for VFV on 2024-01-01 must precede"):
        list(compute_acb(rows))


def test_starts_for_different_tickers_are_independent():
    rows = [
        tx("2023-12-31", "VFV", "START", 100, "95.00"),
        tx("2023-12-31", "XEQT", "START", 200, "27.50"),
        tx("2024-03-10", "VFV", "BUY", 50, "102.00"),
    ]
    out = list(compute_acb(rows))
    vfv = [r["acb"] for r in out if r["ticker"] == "VFV"]
    xeqt = [r["acb"] for r in out if r["ticker"] == "XEQT"]
    assert vfv == [Decimal("9500.00"), Decimal("14600.00")]
    assert xeqt == [Decimal("5500.00")]


def test_cli_end_to_end_on_opening_balance_fixture(tmp_path):
    # START 100 @ 95 → 9500;  BUY 50 @ 102 → 14600 (per-share 97.3333...);
    # SELL 75 @ 110 → ACB removed = 75 * 97.3333... = 7300 → remaining 7300.
    out_path = tmp_path / "out.csv"
    subprocess.run(
        [sys.executable, str(REPO / "acb.py"),
         str(REPO / "sample_with_opening_balance.csv"), "-o", str(out_path)],
        check=True,
    )
    assert out_path.read_text() == (
        "date,ticker,type,quantity,price,acb\n"
        "2023-12-31,VFV,START,100,95.00,9500.00\n"
        "2024-03-10,VFV,BUY,50,102.00,14600.00\n"
        "2024-06-20,VFV,SELL,75,110.00,7300.00\n"
    )


def test_load_transactions_normalizes_case_and_types(tmp_path):
    src = tmp_path / "in.csv"
    src.write_text(
        "date,ticker,type,quantity,price\n"
        "2024-01-15, vfv , buy ,100,98.50\n"
    )
    [row] = load_transactions([str(src)])
    assert row["ticker"] == "VFV"
    assert row["type"] == "BUY"
    assert row["quantity"] == Decimal("100")
    assert row["price"] == Decimal("98.50")

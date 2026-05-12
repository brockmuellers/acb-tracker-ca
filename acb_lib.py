"""Core ACB computation logic shared by acb.py and run.py."""

import csv
import sys
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

YELLOW = "\033[33m"
RESET = "\033[0m"

CENTS = Decimal("0.01")
ONE = Decimal("1")
OUTPUT_COLUMNS = [
    "account_number", "date", "ticker", "type", "quantity", "price",
    "currency", "exchange_rate", "amount_cad", "acb_cad", "gain_loss_cad", "superficial_loss_cad",
]
HOLDINGS_COLUMNS = ["account_number", "ticker", "quantity", "acb_cad"]


def _normalize_time(s):
    """Return zero-padded 'HH:MM' for sorting, or '' if s is empty/None."""
    if not s:
        return ""
    try:
        return datetime.strptime(s, "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ValueError(f"invalid time {s!r} — expected H:MM or HH:MM (e.g. 8:00, 15:30)")


def normalize_rows(rows):
    """Normalize raw string dicts to typed transaction dicts.

    Accepts rows from csv.DictReader or translate_lib output.
    """
    result = []
    for row in rows:
        ticker = row["ticker"].strip().upper()
        date = row["date"].strip()
        currency = ((row.get("currency") or "").strip().upper() or "CAD")
        rate_raw = (row.get("exchange_rate") or "").strip()
        if currency == "CAD":
            exchange_rate = ONE
        else:
            if not rate_raw:
                raise ValueError(
                    f"{ticker} on {date}: non-CAD currency "
                    f"{currency!r} requires an exchange_rate"
                )
            exchange_rate = Decimal(rate_raw)
        time_raw = (row.get("time") or "").strip()
        try:
            time_val = _normalize_time(time_raw)
        except ValueError as e:
            raise ValueError(f"{ticker} on {date}: {e}")
        sq_raw = (row.get("superficial_qty") or "").strip()
        if sq_raw:
            sq = Decimal(sq_raw)
            if sq < 0:
                raise ValueError(
                    f"{ticker} on {date}: superficial_qty must be >= 0 (got {sq})"
                )
            superficial_qty = sq
        else:
            superficial_qty = None
        tx_type = row["type"].strip().upper()
        quantity = Decimal(row["quantity"])
        if tx_type != "TRANSFER" and quantity <= 0:
            raise ValueError(
                f"{ticker} on {date}: quantity must be positive for {tx_type} (got {quantity})"
            )
        if tx_type == "TRANSFER" and quantity == 0:
            raise ValueError(f"{ticker} on {date}: TRANSFER quantity must be non-zero")
        result.append({
            "account_number": (row.get("account_number") or "").strip(),
            "date": date,
            "ticker": ticker,
            "type": tx_type,
            "quantity": quantity,
            "price": Decimal(row["price"]),
            "currency": currency,
            "exchange_rate": exchange_rate,
            "time": time_val,
            "superficial_qty": superficial_qty,
        })
    return result


def load_transactions(paths):
    """Read all input CSVs, normalize, and return a list of dict rows."""
    rows = []
    for path in paths:
        with open(path, newline="") as f:
            rows.extend(csv.DictReader(f))
    return normalize_rows(rows)


def compute_acb(rows):
    """Walk transactions in chronological order, yielding output dicts.

    Per-ticker state is (shares, total_acb), both Decimal and always CAD.
    """
    holdings = {}  # ticker -> [shares, total_acb]
    transfer_out_acb = {}  # (ticker, date, abs_qty) -> [acb_per_share, ...] FIFO

    # Sort by date, then time (empty string sorts before any HH:MM), then
    # TRANSFER-out before TRANSFER-in within the same (date, time), then original input order.
    def _sort_key(p):
        tx = p[1]
        transfer_order = 0 if (tx["type"] == "TRANSFER" and tx["quantity"] < 0) else 1
        return (tx["date"], tx["time"], transfer_order, p[0])

    ordered = sorted(enumerate(rows), key=_sort_key)

    # Warn once per (ticker, date) group that has multiple transactions but incomplete timestamps.
    groups: dict[tuple, list] = {}
    for _, tx in ordered:
        groups.setdefault((tx["ticker"], tx["date"]), []).append(tx)
    for (ticker, date), txs in groups.items():
        types = {tx["type"] for tx in txs}
        # Only warn when types are mixed (e.g. BUY + SELL): same-type groups
        # (all BUY or all SELL) are order-independent and don't need timestamps.
        if len(txs) > 1 and len(types) > 1 and not all(tx["time"] for tx in txs):
            print(
                f"{YELLOW}Warning: mixed transaction types for {ticker} on {date} — "
                f"add a 'time' column to control their order{RESET}",
                file=sys.stderr, flush=True,
            )

    for _, tx in ordered:
        ticker, tx_type = tx["ticker"], tx["type"]
        qty, price = tx["quantity"], tx["price"]
        currency, rate = tx["currency"], tx["exchange_rate"]
        # TODO: double check this, but I think it's most accurate to calculate and round
        # the "amount" before doing the CAD conversion,since the exact dollar value paid
        # is more important than the precise share*price.
        # It may be cleaner to include the rounded amount in the input instead.
        amount_usd = (price * qty).quantize(CENTS, rounding=ROUND_HALF_EVEN)
        amount_cad = amount_usd * rate

        # Warn if a START appears after other transactions for the same ticker.
        # Multiple accounts may legitimately hold the same ticker with separate START rows.
        if tx_type == "START" and ticker in holdings:
            print(
                f"{YELLOW}Warning: START for {ticker} on {tx['date']} follows other "
                f"transactions for that ticker — verify row ordering is correct{RESET}",
                file=sys.stderr, flush=True,
            )

        state = holdings.setdefault(ticker, [Decimal(0), Decimal(0)])
        shares, total_acb = state

        gain_loss = ""
        superficial_loss = ""
        if tx_type in ("START", "BUY"):
            shares += qty
            total_acb += amount_cad
        elif tx_type == "TRANSFER":
            if qty > 0:
                key = (ticker, tx["date"], qty)
                pool = transfer_out_acb.get(key)
                if pool:
                    matched_acb_per_share = pool.pop(0)
                    amount_cad = matched_acb_per_share * qty
                    price = matched_acb_per_share
                else:
                    print(
                        f"{YELLOW}Warning: TRANSFER-in of {qty} shares of {ticker} on "
                        f"{tx['date']} has no matching TRANSFER-out in any tracked account "
                        f"— ensure that 'price' accurately reflects the per-share ACB of "
                        f"the incoming shares{RESET}",
                        file=sys.stderr, flush=True,
                    )
                shares += qty
                total_acb += amount_cad
            else:
                out_qty = -qty
                if out_qty > shares:
                    raise ValueError(
                        f"TRANSFER of {ticker} on {tx['date']} exceeds holdings: {out_qty} > {shares}"
                    )
                acb_per_share = total_acb / shares
                transfer_out_acb.setdefault((ticker, tx["date"], out_qty), []).append(acb_per_share)
                total_acb -= out_qty * acb_per_share
                shares -= out_qty
        elif tx_type == "SELL":
            if qty > shares:
                raise ValueError(
                    f"{tx_type} of {ticker} on {tx['date']} exceeds holdings: {qty} > {shares}"
                )
            # CRA average-cost rule: per-share ACB unchanged by a sell.
            acb_per_share = total_acb / shares
            acb_of_sold = qty * acb_per_share
            total_acb -= acb_of_sold
            shares -= qty

            raw_gain_loss = amount_cad - acb_of_sold

            s_qty = tx["superficial_qty"]
            if s_qty is not None and s_qty > 0:
                if raw_gain_loss >= 0:
                    raise ValueError(
                        f"superficial_qty set for {ticker} on {tx['date']} but the sale is not a loss"
                    )
                if s_qty > qty:
                    raise ValueError(
                        f"superficial_qty {s_qty} exceeds quantity sold {qty} "
                        f"for {ticker} on {tx['date']}"
                    )
                # Denied loss: the proportional loss on the superficial shares.
                # Added back to total_acb so it defers into future per-share cost.
                denied = s_qty * (acb_per_share - amount_cad / qty)
                total_acb += denied
                superficial_loss = denied.quantize(CENTS, rounding=ROUND_HALF_EVEN)
                gain_loss = (raw_gain_loss + denied).quantize(CENTS, rounding=ROUND_HALF_EVEN)
            else:
                gain_loss = raw_gain_loss.quantize(CENTS, rounding=ROUND_HALF_EVEN)
                if s_qty is None and gain_loss < 0:
                    print(
                        f"{YELLOW}Warning: SELL of {qty} shares of {ticker} on {tx['date']} realized a loss of "
                        f"{gain_loss}. If you or an affiliated person bought the same security "
                        f"within 30 days before or after this sale, set superficial_qty to the "
                        f"number of shares repurchased. Set to 0 to confirm no superficial loss.{RESET}",
                        file=sys.stderr, flush=True,
                        )
        elif tx_type == "CASH-IN":
            shares += qty
        elif tx_type == "CASH-OUT":
            # Due to potential settlement delays, cash holdings may sometimes become negative, so don't verify holdings.
            shares -= qty
        else:
            raise ValueError(f"Unknown transaction type: {tx_type!r}")

        # TODO: should total_acb be rounded when saving to state?
        state[0], state[1] = shares, total_acb
        yield {
            "account_number": tx.get("account_number", ""),
            "date": tx["date"],
            "ticker": ticker,
            "type": tx_type,
            "quantity": qty,
            "price": price,
            "currency": currency,
            "exchange_rate": rate,
            "amount_cad": amount_cad,
            "acb_cad": total_acb.quantize(CENTS, rounding=ROUND_HALF_EVEN),
            "gain_loss_cad": gain_loss,
            "superficial_loss_cad": superficial_loss,
        }


def write_csv(rows, out):
    writer = csv.DictWriter(out, fieldnames=OUTPUT_COLUMNS, restval="")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)


def compute_holdings(output_rows):
    """Compute per-account and total holdings from compute_acb output rows.

    Returns a list of dicts with keys: account_number, ticker, quantity, acb_cad.
    Tickers are sorted alphabetically; accounts within each ticker are sorted
    alphabetically with a synthetic TOTAL row last.
    """
    per_account_qty = {}   # (ticker, account) -> Decimal net quantity
    ticker_acb = {}        # ticker -> final acb_cad (from last row for that ticker)
    accounts_seen = {}     # ticker -> set of account_numbers

    for row in output_rows:
        ticker = row["ticker"]
        acct = row["account_number"]
        qty = row["quantity"]
        tx_type = row["type"]

        if ticker not in accounts_seen:
            accounts_seen[ticker] = set()
        accounts_seen[ticker].add(acct)

        key = (ticker, acct)
        if key not in per_account_qty:
            per_account_qty[key] = Decimal(0)

        if tx_type in ("BUY", "START", "CASH-IN"):
            per_account_qty[key] += qty
        elif tx_type in ("SELL", "CASH-OUT"):
            per_account_qty[key] -= qty
        elif tx_type == "TRANSFER":
            per_account_qty[key] += qty  # qty is signed: positive = in, negative = out

        ticker_acb[ticker] = row["acb_cad"]

    # Precompute totals per ticker.
    ticker_total_qty = {
        ticker: sum(per_account_qty[(ticker, a)] for a in accounts_seen[ticker])
        for ticker in ticker_acb
    }

    all_accounts = sorted({a for accts in accounts_seen.values() for a in accts})
    all_tickers = sorted(ticker_acb)

    rows = []
    # Per-account rows: ordered by (account, ticker).
    for acct in all_accounts:
        for ticker in all_tickers:
            if acct not in accounts_seen.get(ticker, set()):
                continue
            total_qty = ticker_total_qty[ticker]
            total_acb = ticker_acb[ticker]
            acct_qty = per_account_qty[(ticker, acct)]
            if total_qty != 0:
                acct_acb = (acct_qty / total_qty * total_acb).quantize(CENTS, rounding=ROUND_HALF_EVEN)
            else:
                acct_acb = Decimal("0.00")
            if acct_qty != 0:
                rows.append({
                    "account_number": acct,
                    "ticker": ticker,
                    "quantity": acct_qty,
                    "acb_cad": acct_acb,
                })

    # TOTAL rows at the end, sorted by ticker.
    for ticker in all_tickers:
        if ticker_total_qty[ticker] != 0:
            rows.append({
                "account_number": "TOTAL",
                "ticker": ticker,
                "quantity": ticker_total_qty[ticker],
                "acb_cad": ticker_acb[ticker],
            })

    return rows


def write_holdings_csv(holdings_rows, out):
    writer = csv.DictWriter(out, fieldnames=HOLDINGS_COLUMNS, restval="")
    writer.writeheader()
    for row in holdings_rows:
        writer.writerow(row)

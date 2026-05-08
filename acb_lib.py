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
    "date", "ticker", "type", "quantity", "price",
    "currency", "exchange_rate", "amount_cad", "acb_cad", "gain_loss_cad", "superficial_loss_cad",
]


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
        result.append({
            "date": date,
            "ticker": ticker,
            "type": row["type"].strip().upper(),
            "quantity": Decimal(row["quantity"]),
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
    # Sort by date, then time (empty string sorts before any HH:MM), then original input order.
    ordered = sorted(enumerate(rows), key=lambda p: (p[1]["date"], p[1]["time"], p[0]))

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
                f"{YELLOW}Warning: mixed BUY and SELL transactions for {ticker} on {date} — "
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

        # A START is only valid as the first appearance of its ticker.
        # This single check covers both ordering ("START came after a
        # BUY/SELL") and uniqueness ("two STARTs for one ticker").
        if tx_type == "START" and ticker in holdings:
            raise ValueError(
                f"START for {ticker} on {tx['date']} must precede "
                f"other transactions for that ticker"
            )

        state = holdings.setdefault(ticker, [Decimal(0), Decimal(0)])
        shares, total_acb = state

        gain_loss = ""
        superficial_loss = ""
        if tx_type in ("START", "BUY"):
            shares += qty
            total_acb += amount_cad
        elif tx_type == "SELL":
            if qty > shares:
                raise ValueError(
                    f"SELL of {ticker} on {tx['date']} exceeds holdings: {qty} > {shares}"
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
        else:
            raise ValueError(f"Unknown transaction type: {tx_type!r}")

        # TODO: should total_acb be rounded when saving to state?
        state[0], state[1] = shares, total_acb
        yield {
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
    writer = csv.DictWriter(out, fieldnames=OUTPUT_COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)

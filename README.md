# acb-tracker-ca

A small Python script that computes the running Adjusted Cost Base (ACB)
for Canadian CRA tax reporting from a CSV of transactions.

## Usage

```
python3 acb.py transactions.csv [more.csv ...] [-o output.csv]
```

Multiple input files are merged and processed in chronological order.
Output goes to stdout unless `-o` is given.

## Input columns

| column          | required | notes                                                                         |
| --------------- | -------- | ----------------------------------------------------------------------------- |
| `date`          | yes      | ISO 8601 (`YYYY-MM-DD`)                                                       |
| `ticker`        | yes      | normalized to upper-case                                                      |
| `type`          | yes      | `START`, `BUY`, or `SELL` (case-insensitive)                                  |
| `quantity`      | yes      | decimal, positive                                                             |
| `price`         | yes      | per-share price in `currency`, decimal, positive                              |
| `currency`      | no       | ISO 4217 (default `CAD`)                                                      |
| `exchange_rate` | no       | foreign-currency-to-CAD rate. Required for non-CAD rows; ignored for CAD rows |

A `START` row declares an opening balance for a ticker — `quantity` is
the shares already held, `price` is their per-share ACB. At most one
`START` per ticker, and it must come before any `BUY`/`SELL` for that
ticker.

## Output columns

`date, ticker, type, quantity, price, currency, exchange_rate, price_cad, acb`

- `price_cad = price * exchange_rate` (raw Decimal product, not quantized)
- `acb` is the running ACB **in CAD** after the transaction, quantized
  to cents using banker's rounding

## Tests

```
python3 -m pytest test_acb.py -v
```

## v1 simplifications

- No commissions / outlays.
- Only `START`, `BUY`, and `SELL` (no DRIP, ROC, splits, phantom distributions).
- No superficial-loss rule.
- No zero-floor handling; over-selling raises a clear error.
- ACB is always reported in CAD; the user supplies the per-row foreign-to-CAD
  exchange rate. No FX rate file lookup, auto-inversion, or cross-currency
  chaining.
- Same-date tie-break is input file order.

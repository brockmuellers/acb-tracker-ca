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

## Translating brokerage exports

Most brokerage CSV exports use different column names and value formats than `acb.py` expects.
`translate.py` bridges that gap using a JSON mapping config:

```
python3 translate.py <broker_export.csv> <mapping.json> [-o output.csv]
                     [--start YYYY-MM-DD] [--end YYYY-MM-DD]
                     [--fx-dir DIR]
```

A ready-to-use mapping for Wealthsimple Trade is in `mappings/wealthsimple.json`.

**Note:** some brokerage exports include a summary block above the actual transaction table
(e.g. Vanguard prepends a portfolio holdings section before the trade history). If the first
row of your CSV is not the correct column header, delete the extra rows manually before running
`translate.py`.

### Automatic exchange rates (`--fx-dir`)

For non-CAD transactions, `acb.py` requires an `exchange_rate` column. `translate.py` can look
this up automatically from Bank of Canada daily FX rate files.

1. Go to <https://www.bankofcanada.ca/rates/exchange/daily-exchange-rates-lookup/>
2. Select the currency pair (e.g. USD/CAD), choose your date range, and download the CSV
3. Save the file to a directory (e.g. `fx_rates/`); repeat for other currencies or date ranges
4. Pass the directory when translating:
   ```
   python3 translate.py broker.csv mappings/wealthsimple.json --fx-dir fx_rates/
   ```

**Note:** some Bank of Canada download pages include introductory rows above the data header.
If the downloaded CSV does not start with `date,FX...`, delete the extra rows manually before
placing the file in your `--fx-dir` directory.

Notes:
- Bank of Canada files cover business days only; weekends and holidays automatically fall back
  to the previous available rate
- Multiple files for the same currency (e.g. different years) are merged automatically
- Every CSV in the directory is expected to be a Bank of Canada FX file

## Tests

```
python3 -m pytest test_acb.py test_translate.py -v
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

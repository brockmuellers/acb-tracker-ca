# acb-tracker-ca

A small Python script that computes the running Adjusted Cost Base (ACB)
for Canadian CRA tax reporting from a CSV of transactions.

### Disclaimer

This tool does not constitute professional tax advice. Use at your own risk.

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
| `time`          | no       | `H:MM` or `HH:MM` (e.g. `8:00`, `15:30`). Tiebreaker when the same ticker has multiple transactions on the same date. If present, all same-day same-ticker rows must have a time or a warning is shown. |

A `START` row declares an opening balance for a ticker — `quantity` is
the shares already held, `price` is their per-share ACB. At most one
`START` per ticker, and it must come before any `BUY`/`SELL` for that
ticker.

Dividend reinvestments (DRIP) should be recorded as `BUY` rows at the
reinvestment price.

## Output columns

`date, ticker, type, quantity, price, currency, exchange_rate, amount_cad, acb_cad, gain_loss_cad`

- `amount_cad = price * quantity * exchange_rate` (total transaction amount in CAD; `price * quantity` is quantized to cents before applying the exchange rate)
- `acb_cad` is the running ACB **in CAD** after the transaction, quantized to cents using banker's rounding
- `gain_loss_cad` is the realized capital gain or loss in CAD for `SELL` transactions (`amount_cad` minus the ACB of the shares sold), quantized to cents. Empty for `BUY` and `START` rows.

## Translating brokerage exports

Most brokerage CSV exports use different column names and value formats than `acb.py` expects.
`translate.py` bridges that gap using a JSON mapping config:

```
python3 translate.py <broker_export.csv> <mapping.json> [-o output.csv]
                     [--start YYYY-MM-DD] [--end YYYY-MM-DD]
                     [--fx-dir DIR]
```

Ready-to-use mappings can be found in `mappings/`.

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

**Note:** Bank of Canada downloads may include introductory rows above the data header.
If the downloaded CSV does not start with `date,FX...`, delete the extra rows manually before
placing the file in your `--fx-dir` directory.

Notes:
- Bank of Canada files cover business days only; weekends and holidays automatically fall back
  to the previous available rate
- Multiple files for the same currency (e.g. different years) are merged automatically
- Every CSV in the directory is expected to be a Bank of Canada FX file

### Sweep transaction types (`sweep_types`)

Some brokers (e.g. Vanguard) store quantity and price in non-standard columns for sweep
transaction types in settlement funds. For example, Vanguard's "Sweep in" and "Sweep out" rows always show
`Shares=0` — the actual dollar amount is in `Net Amount`.

Use `sweep_types` in the mapping config to redirect quantity and/or price for a set of
broker transaction type values:

```json
"sweep_types": {
    "types":          ["Sweep in", "Sweep out"],
    "quantity_col":   "Net Amount",
    "price_override": "1.0"
}
```

- `types` — list of raw broker transaction type values that need alternate column handling
- `quantity_col` — broker column name to use as quantity instead of the mapped column (sign is stripped; absolute value is used)
- `price_override` — literal string to use as price (omit if the mapped price column is already correct)

A ready-to-use Vanguard mapping including this config is in `mappings/vanguard.json`.

### Dates

If a brokerage export includes both "transaction date" and "settlement date", use "settlement date".

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
- No explicit tracking or verification of current holdings by account.
- Transaction notes / comments.
- Row de-duplication.
- ETF conversion (must be treated as BUY/SELL with manually calculated amounts that correctly transfer cost basis.)
# acb-tracker-ca

A small Python script that computes the running Adjusted Cost Base (ACB)
for Canadian CRA tax reporting from CSVs of transactions.

Designed to handle transactions from US brokerages, whose cost basis calculations follow IRS rules, not CRA rules.

### Disclaimer

This tool does not constitute professional tax advice. Use at your own risk.

## Usage

TODO: update with run.py instructions.

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
| `type`          | yes      | `START`, `BUY`, `SELL`, or `TRANSFER` (case-insensitive)                     |
| `quantity`      | yes      | decimal, non-zero. Negative only allowed for `TRANSFER` (transfer out)       |
| `price`         | yes      | per-share price in `currency`, decimal. Ignored for `TRANSFER` with negative quantity |
| `currency`      | no       | ISO 4217 (default `CAD`)                                                      |
| `exchange_rate`     | no       | foreign-currency-to-CAD rate. Required for non-CAD rows; ignored for CAD rows |
| `time`              | no       | `H:MM` or `HH:MM` (e.g. `8:00`, `15:30`). Tiebreaker when the same ticker has multiple transactions on the same date. Accuracy is not important; this is only used for ordering. |
| `superficial_qty`   | no       | Decimal >= 0. Number of shares whose loss is denied under the CRA superficial loss rule (shares repurchased within 30 days before/after this `SELL`). The denied loss is added back to the remaining ACB pool. Set to `0` to confirm no superficial loss and silence the warning. Absent/empty on a loss-generating `SELL` triggers a warning. Only valid on `SELL` rows that realize a loss. |

A `START` row declares an opening balance for a ticker — `quantity` is
the shares already held, `price` is their per-share ACB. At most one
`START` per ticker, and it must come before any `BUY`/`SELL` for that
ticker.

A `TRANSFER` row records securities moved between accounts or brokerages.
Use a **positive** quantity for a transfer in: `price` is the per-share ACB
carried over from the source account, which is added to the running ACB pool.
Use a **negative** quantity for a transfer out: `price` is ignored and the ACB
is reduced proportionally (same rule as a `SELL`). No gain or loss is realized
in either direction. **Limitation:** transfers to or from registered accounts
(RRSP, TFSA) are deemed dispositions at fair market value under CRA rules; model
those as a `SELL` + `BUY` pair instead.

Dividend reinvestments (DRIP) should be recorded as `BUY` rows at the
reinvestment price.

## Output columns

### ACB

`date, ticker, type, quantity, price, currency, exchange_rate, amount_cad, acb_cad, gain_loss_cad, superficial_loss_cad`

- `amount_cad = price * quantity * exchange_rate` (total transaction amount in CAD; `price * quantity` is quantized to cents before applying the exchange rate)
- `acb_cad` is the running ACB **in CAD** after the transaction, quantized to cents using banker's rounding
- `gain_loss_cad` is the realized (non-denied) capital gain or loss in CAD for `SELL` transactions, quantized to cents. Zero for a fully superficial loss. Empty for all other transaction types.
- `superficial_loss_cad` is the denied loss amount in CAD for `SELL` rows where `superficial_qty > 0`, quantized to cents. Empty otherwise.

### Holdings

The program calculates expected holdings across accounts at the end of the transaction period. Useful for checking that all transactions were correctly mapped.

## Translating brokerage exports

Most brokerage CSV exports use different column names and value formats than `acb.py` expects.
`translate.py` bridges that gap using a YAML mapping config:

```
python3 translate.py <broker_export.csv> <mapping.yaml> [-o output.csv]
                     [--start YYYY-MM-DD] [--end YYYY-MM-DD]
                     [--fx-dir DIR]
```

Ready-to-use mappings can be found in `mappings/`.

Brokerage exports date ranges do not need to match the date range of interest; only transactions between the `--start` and `--end` dates will be translated.

**Note:** some brokerage exports include a summary block above the actual transaction table
(e.g. Vanguard prepends a portfolio holdings section before the trade history). If the first
row of your CSV is not the correct column header, delete the extra rows manually before running
`translate.py`.

### Automatic exchange rates (`--fx-dir`)

For non-CAD transactions, `acb.py` requires an `exchange_rate` column. `translate.py` can look
this up from Bank of Canada daily FX rate files.

1. Go to <https://www.bankofcanada.ca/rates/exchange/daily-exchange-rates-lookup/>
2. Select the currency pair (e.g. USD/CAD), choose your date range, and download the CSV
3. Save the file to a directory (e.g. `fx_rates/`); repeat for other currencies or date ranges
4. Pass the directory when translating:
   ```
   python3 translate.py broker.csv mappings/wealthsimple.yaml --fx-dir fx_rates/
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

ACB must be tracked for USD settlement funds, due to fluctuating exchange rates.

Some brokers (e.g. Vanguard) store quantity and price in non-standard columns for sweep
transaction types in settlement funds. For example, Vanguard's "Sweep in" and "Sweep out" rows always show
`Shares=0` — the actual dollar amount is in `Net Amount`.

Use `sweep_types` in the mapping config to redirect quantity and/or price for a set of
broker transaction type values:

```yaml
sweep_types:
  types:
    - Sweep in
    - Sweep out
  quantity_col: Net Amount
  price_override: "1.0"
```

- `types` — list of raw broker transaction type values that need alternate column handling
- `quantity_col` — broker column name to use as quantity instead of the mapped column (sign is stripped; absolute value is used)
- `price_override` — literal price to use (omit if the mapped price column is already correct)

### Cash transaction types (`cash_type_map`)

Some broker transaction types represent actual cash flows — dividends paid in cash, interest, fees, withdrawals. Use `cash_type_map` to route those rows to `CASH-IN` or `CASH-OUT`.

Cash rows always use `ticker = defaults.cash_ticker`, `price = 1.0`, and `quantity = abs(amount_col)`.

```yaml
defaults:
  currency: USD
  cash_ticker: CASH-USD     # ticker assigned to every cash row

cash_type_map:
  quantity_col: Amount      # broker column holding the dollar amount (sign is stripped)
  types:
    Cash Dividend: CASH-IN  # cash-only: not in type_map → one cash row
    Advisor Fee: CASH-OUT   # cash-only: not in type_map → one cash row
    Sell: CASH-IN           # dual: also in type_map → security row + cash row
  ticker_fallback_types:
    Security Transfer: CASH-IN  # see below
```

#### `types` — always-cash routing

Every type listed under `types` always produces a `CASH-IN` or `CASH-OUT` row.

- If the type **also appears in `type_map`** (e.g. `Sell`), one input row produces **two output rows**: a security row (`SELL`) followed by a cash row (`CASH-IN`). This models the double-entry reality of a sale: VTI decreases and cash increases simultaneously.
- If the type **only appears here** (e.g. `Cash Dividend`), only a cash row is produced. The broker row's own ticker is discarded and replaced with `cash_ticker`.

#### `ticker_fallback_types` — ticker-conditional routing

Some broker transaction types can represent either a security movement or a cash movement depending on context. For example, Schwab's "Security Transfer" rows carry a ticker when securities arrive, but have no ticker when the transfer is actually cash.

Types listed under `ticker_fallback_types` are routed based on whether the broker row has a non-empty ticker:

- **Non-empty ticker** → treated as a normal security row via `type_map` (no cash row produced).
- **Empty ticker** → treated as cash only (one `CASH-IN`/`CASH-OUT` row produced, same as `types`).

This is why `ticker_fallback_types` is a separate sub-key rather than a flag on individual entries in `types`: the two sub-keys have meaningfully different semantics, and keeping them visually distinct prevents accidental misconfiguration.

A type may not appear in both `types` and `ticker_fallback_types`.

### Dates

If a brokerage export includes both "transaction date" and "settlement date", use "settlement date".

### Column enrichment

Based on feedback from the ACB calculator, it may be necessary to manually add additional columns to the brokerage export. For example, `time` is added to fix unclear transaction order, and `superficial_qty` is added to designate losses as either allowed or superficial.

### Starting values

Holdings at the start of the period of interest can be manually added in a CSV file as `START` transactions. The "price" column should represent cost basis on the starting date.

## Tests

TODO: update this
```
python3 -m pytest test_acb.py test_translate.py -v
```

## v1 simplifications

- No commissions / outlays.
- Only START, `BUY`, SELL, and TRANSFER are supported
  - DRIP must be modeled as `BUY`/`SELL` pairs.
  - No splits or phantom distributions.
  - Return of Capital (RoC) is not supported. Note that US brokerages may not have unique transaction types representing RoC distributions; instead, distributions may not be classified as RoC until the end of the year. Any RoC amount will be reflected as a non-zero value on box 3 of the 1099-DIV, and a transaction breakdown will be found in the supplemental details.
  - Transfers to or from registered accounts (RRSP, TFSA) are deemed dispositions at FMV; model those as SELL + BUY manually.
  - ETF conversion (assuming we treat ETF conversion as a tax-deferred action) must be treated as BUY/SELL with manually calculated amounts that correctly transfer cost basis.
- Superficial loss rule is user-directed via `superficial_qty`; automatic 30-day window detection is not supported (CRA affiliated-person rules make this impossible to determine from a single account's CSV).
- No zero-floor handling; over-selling raises a clear error.
- No FX rate lookup; the user must supply the file.
- Tracking cash holdings.
- Transaction notes / comments.
- Automated holding verification against an input file.
- Row de-duplication.
- Only "price" is translated for transactions, not "amount", even though "amount" is typically the more important value and less susceptible to rounding inaccuracy.
- Potentially inconsistent currency rounding - values may be inaccurate by a few cents.
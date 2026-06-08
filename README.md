# acb-tracker-ca

A small, stateless Python script that computes the running Adjusted Cost Base (ACB) for Canadian CRA tax reporting from CSVs of transactions. Designed for masochistic self-filing expats.

Focuses on handling transactions from US brokerages, whose cost basis calculations follow IRS rules, not CRA rules.

Although there are several online tools that will calculate ACB, they don't show their work, and generally don't provide easy ways to double check that transactions have been imported and translated correctly. This tool provides an extra layer of confidence when reporting gain/loss to the CRA.

### Disclaimer

This tool does not constitute professional tax advice, and is heavily optimized for my personal use case. Use at your own risk. A cross-border tax professional is always the best resource, but amateur tax enthusiasts may enjoy the excellent (and free!) [MyACB](https://myacb.ca).

## Usage

Calculate ACB directly from brokerage exports:

```.sh
python3 run.py <config.yaml> [-o output.csv] [-p]
```

Or, run the equivalent:

```.sh
# Optional: translate brokerage export CSVs to normalized format
python3 translate.py <input_csv> <mapping_config>
# Generate the ACB data
python3 acb.py transactions.csv [...]
```

See the docstring for each of these scripts for more usage instructions.

## Input

### Raw brokerage exports

`run.py` and `translate.py` take raw brokerage files as input, in addition to mapping files that define how brokerage-specific formats map to the ACB calculator's expected input types.

More information on translation can be found later in this README.

### Normalized input format

The `acb.py` script requires CSV input files in a normalized format:

* Required: `date`, `ticker`, `type`, `quantity`, `price`
* Optional: `currency`, `exchange_rate`, `time` (ordering tiebreaker), `superficial_qty` (for rows with superficial loss)

### Initial holdings

Holdings at the start of the period of interest (e.g. residency start date) can be manually added in a CSV file as `START` transactions. The "price" column should represent cost basis on the starting date.

## Output

### ACB

The script outputs a table printed to stdout, and optionally a CSV. The output contains all rows from the input, as well as the following calculations:

- `amount_cad = price * quantity * exchange_rate` (total transaction amount in CAD)
- `acb_cad` is the running ACB in CAD after the transaction, quantized to cents using banker's rounding.
- `gain_loss_cad` is the realized (non-denied) capital gain or loss in CAD for `SELL` transactions, rounded to cents. Zero for a fully superficial loss.
- `superficial_loss_cad` is the denied loss amount in CAD for `SELL` rows where `superficial_qty > 0`, rounded to cents. Empty otherwise.

### Holdings

For convenience, the program calculates expected holdings across accounts at the end of the transaction period. Useful for checking that all transactions were correctly mapped. Similar to ACB, this may be printed to stdout or written to CSV.

## Translating brokerage exports

Most brokerage CSV exports use different column names and value formats than `acb.py` expects. `translate.py` bridges that gap using a YAML mapping config.

Brokerage export date ranges do not need to match the date range of interest; only transactions between the `--start` and `--end` dates will be translated.

**Note:** some brokerage exports include a summary block above the actual transaction table
(e.g. Vanguard prepends a portfolio holdings section before the trade history). If the first
row of your CSV is not the correct column header, delete the extra rows manually before running
`translate.py`.

### Mapping config

Use a yaml file to define the mapping of brokerage export columns to the normalized input columns. Documentation on the format can be found in the `translate.py` docstring, and sample mappings can be found in `mappings/`.

A few special cases:
* **Dates**: If multiple dates are provided for a transaction, use settlement date.
* **Settlement funds**: ACB must be tracked for USD settlement funds that are fixed to the value of the US dollar, due to fluctuating exchange rates. Some brokers store quantity and price in non-standard columns for sweep transaction types in settlement funds. For example, Vanguard's "Sweep in" and "Sweep out" rows always show empty values for share quantity, and the actual dollar amount is in `Net Amount`. Use `settlement_fund_types` in the mapping config to redirect quantity and/or price for a set of
broker transaction type values.
* **Cash transactions**: Some broker transaction types represent actual cash flows — dividends paid in cash, interest, fees, withdrawals. These are not important for ACB tracking, but do help when verifying holdings. Use `cash_type_map` to route those rows to `CASH-IN` or `CASH-OUT`. Cash rows always use `ticker = defaults.cash_ticker`, `price = 1.0`, and `quantity = abs(amount_col)`.
  * If a type is listed under `cash_type_map.types`, and also under the top level `type_map` (e.g. mapped to `SELL`), then one input row produces two output rows: the expected security row (e.g. `SELL`, representing the decrease in security holdings), and the cash row (e.g. `CASH-IN` with ticker `CASH-USD`, representing the influx of cash). This models the double-entry reality of certain brokerage transactions. Types that ONLY appear in `cash_type_map.types` (e.g. `Cash Dividend`) only produce a single cash row.
  * Some broker transaction types can represent either a security movement or a cash movement depending on context. For example, Schwab's "Security Transfer" rows carry a ticker when securities arrive, but have no ticker when the transfer is actually cash. Use `ticker_fallback_types` to specify when a blank ticker corresponds to a cash transaction.

### Automatic exchange rates (`--fx-dir`)

For non-CAD transactions, `acb.py` requires an `exchange_rate` column. This should correspond to the exchange rate on the settlement date of the transaction. `translate.py` can look
this up from Bank of Canada daily FX rate files.

1. Go to <https://www.bankofcanada.ca/rates/exchange/daily-exchange-rates-lookup/>
2. Select the currency pair (e.g. USD/CAD), choose your date range, and download the CSV
3. Save the file to a directory (e.g. `fx_rates/`); repeat for other currencies or date ranges
4. Manually remove any introductory rows above the data header. The header should look like `date,FX...`.
5. Pass this directory to the `translate.py` script to auto-populate exchange rates.

### Column enrichment

Based on feedback from the ACB calculator, it may be necessary to manually add additional columns to the brokerage export. For example, `time` is added to fix unclear transaction order, and `superficial_qty` is added to designate losses as either allowed or superficial.

## Limitations

Certain complex situations are not directly modeled by this tool, but the user should be aware of them:

* **RoC distributions:** US brokerages may not have unique transaction types representing RoC distributions; instead, distributions may not be classified as RoC until the end of the year. Any RoC amount will be reflected as a non-zero value on box 3 of the 1099-DIV, and a transaction breakdown will be found in the supplemental details.
* **Tax-free account transfers:** Transfers to or from registered tax-deferred accounts (RRSP, TFSA) are deemed dispositions at FMV, so must be modeled as SELL + BUY manually.
* **ETF conversion:** There is not a consensus on whether ETF conversion is a tax-deferred action or a deemed disposition. If one chooses the tax-deferred interpretation, ETF conversions must be treated as BUY/SELL with manually calculated amounts that correctly transfer cost basis. A deemed disposition can be modeled as a BUY/SELL pair using FMV.
* **Superficial loss:** The rules surrounding superficial losses are quite complicated. It is not possible for this tool to automatically detect superficial losses due to the CRA affiliated-person rules, and potential transactions in untracked accounts (e.g. retirement accounts).
* **Fees:** Investment management fees can be claimed on a tax return, but that is not handled by this tool.
* **Unsupported transaction types:** In addition to ETF conversion and management fees, the following transaction types have no special handling: DRIP (dividend reinvestment, which must be modeled as BUY/SELL pairs), splits and reverse splits, phantom distributions (not relevant to US brokerages)

A few other limitations:

- Only "price" is translated for transactions, not "amount", even though "amount" is typically the more important value and less susceptible to rounding inaccuracy
- Potential inconsistent currency rounding - values may be inaccurate by a few cents, compared to other tools. Generally not an issue for tax filing, as those values are rounded to the nearest dollar, but it is bothersome
- No automated FX rate lookup; the user must supply the file
- Transaction notes / comments are not propogated to the output
- Rows are not de-duplicated


## Tests

As these numbers are provided to the CRA, the scripts are well-tested.

```
python3 -m pytest test_acb.py test_translate.py test_run.py -v
```
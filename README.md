# Car Spreadsheet Comparer

A small Python command-line tool for fetching a Google Sheets car spreadsheet and comparing rows with filters, sorting, selected columns, and CSV or Markdown output.

The default source is:

https://docs.google.com/spreadsheets/d/1V6ucyFGKWuSQzvI8lMzvvWJHrBS82echMVJH37kwgjE/edit?gid=2069101638#gid=2069101638

## Requirements

- Python 3.8+
- No third-party Python packages
- The spreadsheet must be shared so it can be exported as CSV

## Usage

List available columns:

```bash
python3 car_compare.py --list-columns
```

Show the best dry summer rows by average:

```bash
python3 car_compare.py --filter Season=Summer --filter Surface=Dry --sort Average --limit 10
```

Compare specific cars by name fragment:

```bash
python3 car_compare.py --compare "Volvo ES90" "Nio EL8" --columns Car,Tires,Average,"100 km/h"
```

Filter by numeric ranges:

```bash
python3 car_compare.py --min "Average=63" --max "Average=65" --sort Average
```

Use a local CSV file instead of Google Sheets:

```bash
python3 car_compare.py --source cars.csv --filter Tires=Michelin
```

Export filtered results as CSV:

```bash
python3 car_compare.py --filter Surface=Dry --output csv > filtered-cars.csv
```

## Options

- `--source`: Google Sheets URL, CSV export URL, or local CSV path.
- `--filter COLUMN=TEXT`: Keep rows where `COLUMN` contains `TEXT`. Repeatable.
- `--min COLUMN=NUMBER`: Keep rows where numeric `COLUMN` is at least `NUMBER`. Repeatable.
- `--max COLUMN=NUMBER`: Keep rows where numeric `COLUMN` is at most `NUMBER`. Repeatable.
- `--compare CAR_TEXT ...`: Show cars whose `Car` column contains any of the provided values.
- `--sort COLUMN`: Sort by a column. Numeric columns sort numerically.
- `--desc`: Sort descending.
- `--limit NUMBER`: Limit shown rows. Use `0` for all rows.
- `--columns A,B,C`: Choose columns to display.
- `--output table|markdown|csv`: Choose output format.

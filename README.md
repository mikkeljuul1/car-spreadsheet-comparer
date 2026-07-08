# Car Spreadsheet Comparer

A small Python tool for fetching a multi-tab Google Sheets car spreadsheet and comparing cars in a local website or from the command line.

The default source is:

https://docs.google.com/spreadsheets/d/1V6ucyFGKWuSQzvI8lMzvvWJHrBS82echMVJH37kwgjE

Spreadsheet data is created and maintained by [Bjørn Nyland](https://www.youtube.com/@bjornnyland).

## Requirements

- Python 3.8+
- No third-party Python packages
- The spreadsheet must be shared so it can be exported as CSV

## Website Usage

Open the local website:

```bash
python3 car_compare.py
```

Or open it explicitly without launching a browser:

```bash
python3 car_compare.py --web --no-browser
```

The website discovers the visible tabs in the Google Sheet, loads each tab, and lets you compare selected cars across all selected tests. The default workbook currently includes tabs such as Weight, Acceleration, Noise, Braking, Range, 1000 km, Geilo, Degradation, Zero mile, 500 km, Arctic Circle, and Bangkok.

Use a different spreadsheet source:

```bash
python3 car_compare.py --web --source "https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit?gid=123#gid=123"
```

Legacy desktop GUI:

```bash
python3 car_compare.py --gui
```

The website lets you:

- Search and choose cars once.
- Turn spreadsheet tabs on or off as test groups.
- Compare metrics with cars as columns and tests as sections.
- See winner summaries, detailed comparison tables, and raw spreadsheet rows.

## GitHub Pages

Build the static site for GitHub Pages:

```bash
python3 car_compare.py --build-static docs
```

This creates:

- `docs/index.html`: the static website.
- `docs/workbook.json`: a snapshot of all spreadsheet tabs.

To publish it for free:

1. Push this repository to GitHub.
2. Open the repository settings on GitHub.
3. Go to Pages.
4. Set the source to deploy from the `main` branch and the `/docs` folder.
5. Save and wait for GitHub Pages to publish the site.

The included GitHub Actions workflow refreshes `docs/workbook.json` daily and can also be run manually from the Actions tab.

## Analytics

The static website includes Plausible analytics support. Analytics is disabled on localhost and enabled on the GitHub Pages host `mikkeljuul1.github.io`.

To activate it, create a Plausible site for `mikkeljuul1.github.io`. Plausible will then track normal site analytics such as visits, page views, referrers, countries, devices, browsers, and visit duration.

The app also sends these custom events:

- `workbook_loaded`: includes loaded tab count, total tab count, row count, static/live source, and snapshot timestamp.
- `car_selected`: includes selected car name and selected car count.
- `test_toggled`: includes test name, enabled/disabled state, and enabled test count.
- `comparison_generated`: includes selected car names, enabled test names, car count, test count, category count, and scored category count.

## Command-Line Usage

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
- `--web`: Open the local website comparer.
- `--gui`: Open the legacy desktop GUI.
- `--host`: Host for the website. Default: `127.0.0.1`.
- `--port`: Starting port for the website. Default: `8765`.
- `--no-browser`: Start the website without opening a browser.
- `--export-workbook PATH`: Export all spreadsheet tabs to a static workbook JSON file.
- `--build-static DIR`: Build a static GitHub Pages site into `DIR`.
- `--filter COLUMN=TEXT`: Keep rows where `COLUMN` contains `TEXT`. Repeatable.
- `--min COLUMN=NUMBER`: Keep rows where numeric `COLUMN` is at least `NUMBER`. Repeatable.
- `--max COLUMN=NUMBER`: Keep rows where numeric `COLUMN` is at most `NUMBER`. Repeatable.
- `--compare CAR_TEXT ...`: Show cars whose `Car` column contains any of the provided values.
- `--sort COLUMN`: Sort by a column. Numeric columns sort numerically.
- `--desc`: Sort descending.
- `--limit NUMBER`: Limit shown rows. Use `0` for all rows.
- `--columns A,B,C`: Choose columns to display.
- `--output table|markdown|csv`: Choose output format.

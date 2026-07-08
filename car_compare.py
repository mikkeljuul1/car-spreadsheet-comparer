#!/usr/bin/env python3
"""
Fetch and compare car data from a Google Sheets CSV export.

Examples:
    python3 car_compare.py --filter Season=Summer --filter Surface=Dry --sort Average --limit 10
    python3 car_compare.py --compare "Volvo ES90" "Nio EL8" --columns Car,Tires,Average,"100 km/h"
    python3 car_compare.py --min "Average=63" --max "Average=65" --sort Average
"""

import argparse
import csv
import io
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_SOURCE = (
    "https://docs.google.com/spreadsheets/d/1V6ucyFGKWuSQzvI8lMzvvWJHrBS82echMVJH37kwgjE/"
    "edit?gid=2069101638#gid=2069101638"
)
DEFAULT_COLUMNS = ["Car", "Surface", "Tires", "Season", "Size front", "Size rear", "80 km/h", "100 km/h", "120 km/h", "Average"]
NUMERIC_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
URL_RE = re.compile(r"^https?://", re.IGNORECASE)
MAX_TABLE_COLUMN_WIDTH = 36


def spreadsheet_csv_url(source):
    """Convert a Google Sheets edit URL into its CSV export URL."""
    parsed = urllib.parse.urlparse(source)
    if not parsed.netloc.endswith("docs.google.com") or "/spreadsheets/d/" not in parsed.path:
        return source

    path_parts = parsed.path.strip("/").split("/")
    try:
        spreadsheet_id = path_parts[path_parts.index("d") + 1]
    except (ValueError, IndexError) as error:
        raise ValueError(f"Could not find spreadsheet id in URL: {source}") from error

    query = urllib.parse.parse_qs(parsed.query)
    fragment = urllib.parse.parse_qs(parsed.fragment)
    gid = (query.get("gid") or fragment.get("gid") or ["0"])[0]
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv&gid={gid}"


def load_csv_text(source):
    if URL_RE.match(source):
        request = urllib.request.Request(
            spreadsheet_csv_url(source),
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
            content_type = response.headers.get("Content-Type", "")
        text = data.decode("utf-8-sig")
        if "text/html" in content_type and "<html" in text[:500].lower():
            raise RuntimeError("Google returned HTML instead of CSV. Check that the sheet is shared for reading.")
        return text

    if not os.path.exists(source):
        raise FileNotFoundError(source)
    with open(source, "r", encoding="utf-8-sig", newline="") as csv_file:
        return csv_file.read()


def clean_cell(value):
    if value is None:
        return ""

    compact = re.sub(r"\s+", "", value.strip()).replace(",", ".")
    if NUMERIC_RE.match(compact):
        return compact

    return " ".join(value.split())


def numeric_value(value):
    compact = re.sub(r"\s+", "", value.strip()).replace(",", ".")
    if not NUMERIC_RE.match(compact):
        return None
    return float(compact)


def load_rows(source):
    csv_text = load_csv_text(source)
    reader = csv.reader(io.StringIO(csv_text))
    try:
        raw_headers = next(reader)
    except StopIteration:
        return [], []

    headers = [clean_cell(header) for header in raw_headers]
    rows = []
    for raw_row in reader:
        padded_row = raw_row + [""] * (len(headers) - len(raw_row))
        row = {header: clean_cell(padded_row[index]) for index, header in enumerate(headers) if header}
        if any(row.values()):
            rows.append(row)
    return headers, rows


def normalized_column_name(value):
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def resolve_column(headers, requested):
    requested_key = normalized_column_name(requested)
    for header in headers:
        if normalized_column_name(header) == requested_key:
            return header
    raise ValueError(f"Unknown column '{requested}'. Available columns: {', '.join(headers)}")


def split_assignment(expression):
    if "=" not in expression:
        raise ValueError(f"Expected COLUMN=VALUE, got '{expression}'")
    column, value = expression.split("=", 1)
    column = column.strip()
    value = value.strip()
    if not column:
        raise ValueError(f"Missing column name in '{expression}'")
    return column, value


def split_columns(value):
    return [column.strip() for column in value.split(",") if column.strip()]


def apply_text_filters(rows, headers, filters):
    checks = []
    for expression in filters:
        column, value = split_assignment(expression)
        checks.append((resolve_column(headers, column), value.casefold()))

    return [row for row in rows if all(value in row.get(column, "").casefold() for column, value in checks)]


def apply_numeric_filters(rows, headers, minimums, maximums):
    lower_bounds = []
    upper_bounds = []

    for expression in minimums:
        column, value = split_assignment(expression)
        threshold = numeric_value(value)
        if threshold is None:
            raise ValueError(f"Minimum for '{column}' must be numeric, got '{value}'")
        lower_bounds.append((resolve_column(headers, column), threshold))

    for expression in maximums:
        column, value = split_assignment(expression)
        threshold = numeric_value(value)
        if threshold is None:
            raise ValueError(f"Maximum for '{column}' must be numeric, got '{value}'")
        upper_bounds.append((resolve_column(headers, column), threshold))

    def passes(row):
        for column, threshold in lower_bounds:
            value = numeric_value(row.get(column, ""))
            if value is None or value < threshold:
                return False
        for column, threshold in upper_bounds:
            value = numeric_value(row.get(column, ""))
            if value is None or value > threshold:
                return False
        return True

    return [row for row in rows if passes(row)]


def apply_compare(rows, patterns):
    if not patterns:
        return rows

    lowered_patterns = [pattern.casefold() for pattern in patterns]
    matched_rows = [row for row in rows if any(pattern in row.get("Car", "").casefold() for pattern in lowered_patterns)]

    for pattern, lowered_pattern in zip(patterns, lowered_patterns):
        if not any(lowered_pattern in row.get("Car", "").casefold() for row in matched_rows):
            print(f"warning: no car matched '{pattern}'", file=sys.stderr)

    return matched_rows


def sort_rows(rows, headers, sort_column, descending):
    if not sort_column:
        return rows

    column = resolve_column(headers, sort_column)

    def sort_key(row):
        value = row.get(column, "")
        number = numeric_value(value)
        if number is None:
            return (1, value.casefold())
        return (0, number)

    return sorted(rows, key=sort_key, reverse=descending)


def display_columns(headers, requested_columns):
    if requested_columns:
        return [resolve_column(headers, column) for column in split_columns(requested_columns)]
    return [column for column in DEFAULT_COLUMNS if column in headers]


def shorten(value, width):
    if len(value) <= width:
        return value
    if width <= 3:
        return value[:width]
    return value[: width - 3] + "..."


def table_output(rows, columns):
    if not rows:
        return "No cars matched."

    widths = {}
    numeric_columns = set()
    for column in columns:
        values = [row.get(column, "") for row in rows]
        widths[column] = min(MAX_TABLE_COLUMN_WIDTH, max([len(column), *[len(value) for value in values]]))
        non_empty_values = [value for value in values if value]
        if non_empty_values and all(numeric_value(value) is not None for value in non_empty_values):
            numeric_columns.add(column)

    def format_value(column, value):
        shortened = shorten(value, widths[column])
        if column in numeric_columns:
            return shortened.rjust(widths[column])
        return shortened.ljust(widths[column])

    header = " | ".join(format_value(column, column) for column in columns)
    separator = "-+-".join("-" * widths[column] for column in columns)
    body = [" | ".join(format_value(column, row.get(column, "")) for column in columns) for row in rows]
    return "\n".join([header, separator, *body])


def markdown_output(rows, columns):
    if not rows:
        return "No cars matched."

    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for row in rows:
        values = [row.get(column, "").replace("|", "\\|") for column in columns]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def csv_output(rows, columns):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().rstrip()


def build_parser():
    parser = argparse.ArgumentParser(
        description="Fetch a car spreadsheet and compare rows with filters, sorting, and selected columns.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --filter Season=Summer --filter Surface=Dry --sort Average --limit 10
  %(prog)s --compare "Volvo ES90" "Nio EL8" --columns Car,Tires,Average,"100 km/h"
  %(prog)s --min "Average=63" --max "Average=65" --sort Average --output markdown
  %(prog)s --source cars.csv --filter Tires=Michelin
""",
    )
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="Google Sheets URL, CSV export URL, or local CSV path.")
    parser.add_argument("--filter", action="append", default=[], metavar="COLUMN=TEXT", help="Keep rows where COLUMN contains TEXT. Repeatable.")
    parser.add_argument("--min", dest="minimums", action="append", default=[], metavar="COLUMN=NUMBER", help="Keep rows where numeric COLUMN is at least NUMBER. Repeatable.")
    parser.add_argument("--max", dest="maximums", action="append", default=[], metavar="COLUMN=NUMBER", help="Keep rows where numeric COLUMN is at most NUMBER. Repeatable.")
    parser.add_argument("--compare", nargs="+", metavar="CAR_TEXT", help="Show cars whose Car column contains any of these values.")
    parser.add_argument("--sort", metavar="COLUMN", help="Sort by COLUMN. Numeric columns sort numerically.")
    parser.add_argument("--desc", action="store_true", help="Sort descending.")
    parser.add_argument("--limit", type=int, default=20, help="Maximum rows to show. Use 0 for all rows. Default: 20.")
    parser.add_argument("--columns", help="Comma-separated columns to display. Default: common comparison columns.")
    parser.add_argument("--output", choices=("table", "markdown", "csv"), default="table", help="Output format. Default: table.")
    parser.add_argument("--list-columns", action="store_true", help="Print available columns and exit.")
    return parser


def run(args):
    headers, rows = load_rows(args.source)
    if args.list_columns:
        print("\n".join(headers))
        return 0

    rows = apply_text_filters(rows, headers, args.filter)
    rows = apply_numeric_filters(rows, headers, args.minimums, args.maximums)
    rows = apply_compare(rows, args.compare)
    rows = sort_rows(rows, headers, args.sort, args.desc)
    if args.limit > 0:
        rows = rows[: args.limit]

    columns = display_columns(headers, args.columns)
    if args.output == "csv":
        print(csv_output(rows, columns))
    elif args.output == "markdown":
        print(markdown_output(rows, columns))
    else:
        print(table_output(rows, columns))
    return 0


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        return run(args)
    except (csv.Error, FileNotFoundError, RuntimeError, urllib.error.URLError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

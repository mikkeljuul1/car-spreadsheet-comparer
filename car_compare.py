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
import datetime
import html
import http.server
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    tk = None
    filedialog = None
    messagebox = None
    ttk = None


DEFAULT_SOURCE = "https://docs.google.com/spreadsheets/d/1V6ucyFGKWuSQzvI8lMzvvWJHrBS82echMVJH37kwgjE"
DEFAULT_COLUMNS = ["Car", "Surface", "Tires", "Season", "Size front", "Size rear", "80 km/h", "100 km/h", "120 km/h", "Average"]
COMMON_FILTER_COLUMNS = ("Surface", "Season", "Tires")
NUMERIC_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
URL_RE = re.compile(r"^https?://", re.IGNORECASE)
SHEET_TAB_RE = re.compile(r'\[21350203,"\[(\d+),0,\\"(\d+)\\",.*?\[\[0,0,\\"([^\\"]+)\\"\]', re.DOTALL)
MAX_TABLE_COLUMN_WIDTH = 36


def spreadsheet_id_from_source(source):
    parsed = urllib.parse.urlparse(source)
    if not parsed.netloc.endswith("docs.google.com") or "/spreadsheets/d/" not in parsed.path:
        return None

    path_parts = parsed.path.strip("/").split("/")
    try:
        return path_parts[path_parts.index("d") + 1]
    except (ValueError, IndexError) as error:
        raise ValueError(f"Could not find spreadsheet id in URL: {source}") from error


def gid_from_source(source, default="0"):
    parsed = urllib.parse.urlparse(source)
    query = urllib.parse.parse_qs(parsed.query)
    fragment = urllib.parse.parse_qs(parsed.fragment)
    return (query.get("gid") or fragment.get("gid") or [default])[0]


def fetch_url_bytes(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read(), response.headers.get("Content-Type", "")
    except urllib.error.URLError as error:
        if "CERTIFICATE_VERIFY_FAILED" not in str(error) or shutil.which("curl") is None:
            raise
        completed = subprocess.run(
            ["curl", "-L", "--fail", "--silent", "--show-error", "--max-time", "30", url],
            check=False,
            capture_output=True,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip() or str(error)
            raise RuntimeError(f"Could not fetch URL: {detail}") from error
        return completed.stdout, ""


def spreadsheet_csv_url(source):
    """Convert a Google Sheets edit URL into its CSV export URL."""
    spreadsheet_id = spreadsheet_id_from_source(source)
    if spreadsheet_id is None:
        return source
    gid = gid_from_source(source)
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv&gid={gid}"


def spreadsheet_sheet_url(source, gid):
    spreadsheet_id = spreadsheet_id_from_source(source)
    if spreadsheet_id is None:
        return source
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv&gid={gid}"


def load_csv_text(source):
    if URL_RE.match(source):
        csv_url = spreadsheet_csv_url(source)
        data, content_type = fetch_url_bytes(csv_url)
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
    default_columns = [column for column in DEFAULT_COLUMNS if column in headers]
    if len(default_columns) > 1:
        return default_columns
    return headers[: min(10, len(headers))]


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


def discover_sheets(source):
    spreadsheet_id = spreadsheet_id_from_source(source)
    if spreadsheet_id is None:
        return [{"index": 0, "gid": "local", "title": os.path.basename(source) or "Local CSV"}]

    edit_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit?usp=sharing"
    data, _content_type = fetch_url_bytes(edit_url)
    page = data.decode("utf-8", errors="replace")
    sheets = []
    seen_gids = set()
    for match in SHEET_TAB_RE.finditer(page):
        index_text, gid, escaped_title = match.groups()
        if gid in seen_gids:
            continue
        try:
            title = json.loads(f'"{escaped_title}"')
        except json.JSONDecodeError:
            title = escaped_title
        sheets.append({"index": int(index_text), "gid": gid, "title": title})
        seen_gids.add(gid)

    if sheets:
        return sorted(sheets, key=lambda sheet: sheet["index"])

    gid = gid_from_source(source)
    return [{"index": 0, "gid": gid, "title": f"Sheet {gid}"}]


def load_workbook(source):
    sheets = []
    for sheet in discover_sheets(source):
        sheet_source = spreadsheet_sheet_url(source, sheet["gid"])
        sheet_data = dict(sheet)
        try:
            headers, rows = load_rows(sheet_source)
            sheet_data.update({"headers": headers, "rows": rows, "rowCount": len(rows), "error": None})
        except (csv.Error, FileNotFoundError, RuntimeError, urllib.error.URLError, ValueError) as error:
            sheet_data.update({"headers": [], "rows": [], "rowCount": 0, "error": str(error)})
        sheets.append(sheet_data)
    return {"source": source, "sheets": sheets}


def workbook_summary(workbook):
    sheet_count = len(workbook["sheets"])
    loaded_count = sum(1 for sheet in workbook["sheets"] if not sheet.get("error"))
    row_count = sum(sheet.get("rowCount", 0) for sheet in workbook["sheets"])
    return {"sheetCount": sheet_count, "loadedSheetCount": loaded_count, "rowCount": row_count}


def static_workbook(source):
    workbook = load_workbook(source)
    workbook["summary"] = workbook_summary(workbook)
    workbook["generatedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    return workbook


def export_workbook(source, output_path):
    workbook = static_workbook(source)
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as output_file:
        json.dump(workbook, output_file, ensure_ascii=False, separators=(",", ":"))
    return workbook


def build_static_site(source, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    app_path = os.path.join(os.path.dirname(__file__), "web_compare.html")
    index_path = os.path.join(output_dir, "index.html")
    workbook_path = os.path.join(output_dir, "workbook.json")
    shutil.copyfile(app_path, index_path)
    workbook = export_workbook(source, workbook_path)
    return index_path, workbook_path, workbook


class CompareWebRequestHandler(http.server.BaseHTTPRequestHandler):
    server_version = "CarCompareWeb/1.0"

    def log_message(self, format_text, *args):
        print(f"{self.address_string()} - {format_text % args}")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send_web_app()
            return
        if parsed.path == "/api/workbook":
            self._send_workbook(parsed)
            return
        self.send_error(404, "Not found")

    def _send_web_app(self):
        app_path = os.path.join(os.path.dirname(__file__), "web_compare.html")
        try:
            with open(app_path, "rb") as app_file:
                body = app_file.read()
        except OSError as error:
            self.send_error(500, f"Could not read web app: {html.escape(str(error))}")
            return
        self._send_bytes(body, "text/html; charset=utf-8")

    def _send_workbook(self, parsed):
        query = urllib.parse.parse_qs(parsed.query)
        source = (query.get("source") or [self.server.source])[0].strip() or DEFAULT_SOURCE
        refresh = (query.get("refresh") or ["0"])[0] == "1"

        with self.server.cache_lock:
            cached = self.server.workbook_cache.get(source)
            if cached is None or refresh:
                cached = load_workbook(source)
                cached["summary"] = workbook_summary(cached)
                self.server.workbook_cache[source] = cached

        self._send_json(cached)

    def _send_json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def available_port(host, preferred_port):
    for port in range(preferred_port, preferred_port + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            try:
                candidate.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"Could not find an available port starting at {preferred_port}")


def launch_web(initial_source=DEFAULT_SOURCE, host="127.0.0.1", port=8765, open_browser=True):
    selected_port = available_port(host, port)
    server = http.server.ThreadingHTTPServer((host, selected_port), CompareWebRequestHandler)
    server.source = initial_source
    server.workbook_cache = {}
    server.cache_lock = threading.Lock()
    url = f"http://{host}:{selected_port}/"
    print(f"Car comparer website running at {url}")
    print("Press Ctrl+C to stop the server.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping car comparer website.")
    finally:
        server.server_close()


class CarCompareApp:
    def __init__(self, root, initial_source):
        self.root = root
        self.root.title("Car Spreadsheet Comparer")
        self.root.geometry("1240x780")
        self.root.minsize(980, 620)

        self.headers = []
        self.rows = []
        self.filtered_rows = []
        self.current_columns = []
        self.all_car_names = []
        self.visible_car_names = []
        self.selected_cars = set()
        self.text_filters = []
        self.range_filters = []
        self.updating_car_list = False

        self.source_var = tk.StringVar(value=initial_source)
        self.car_search_var = tk.StringVar()
        self.text_filter_column_var = tk.StringVar()
        self.text_filter_value_var = tk.StringVar()
        self.range_column_var = tk.StringVar()
        self.range_min_var = tk.StringVar()
        self.range_max_var = tk.StringVar()
        self.sort_var = tk.StringVar()
        self.desc_var = tk.BooleanVar(value=False)
        self.limit_var = tk.StringVar(value="50")
        self.status_var = tk.StringVar(value="Ready")
        self.common_filter_vars = {column: tk.StringVar(value="All") for column in COMMON_FILTER_COLUMNS}

        self._configure_style()
        self._build_ui()
        self.car_search_var.trace_add("write", lambda *_: self._update_car_list())
        self.refresh_data()

    def _configure_style(self):
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        self.root.configure(bg="#f4f1ea")
        style.configure("TFrame", background="#f4f1ea")
        style.configure("TLabelframe", background="#f4f1ea", bordercolor="#d6cfc2")
        style.configure("TLabelframe.Label", background="#f4f1ea", foreground="#263238", font=("Avenir Next", 11, "bold"))
        style.configure("TLabel", background="#f4f1ea", foreground="#263238", font=("Avenir Next", 10))
        style.configure("Title.TLabel", font=("Avenir Next", 20, "bold"), foreground="#19323c")
        style.configure("Muted.TLabel", foreground="#65727a")
        style.configure("Accent.TButton", font=("Avenir Next", 10, "bold"), foreground="#102027")
        style.configure("Treeview", rowheight=28, font=("Avenir Next", 10), fieldbackground="#fffdf8", background="#fffdf8")
        style.configure("Treeview.Heading", font=("Avenir Next", 10, "bold"), background="#e4ded2", foreground="#263238")
        style.map("Treeview", background=[("selected", "#2f5d62")], foreground=[("selected", "#ffffff")])

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=14)
        main.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        header = ttk.Frame(main)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Car Spreadsheet Comparer", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="Choose cars, filter test rows, sort results, and export the table.", style="Muted.TLabel").grid(row=1, column=0, sticky="w")

        source_frame = ttk.LabelFrame(main, text="Spreadsheet", padding=10)
        source_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        source_frame.columnconfigure(0, weight=1)
        ttk.Entry(source_frame, textvariable=self.source_var).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.load_button = ttk.Button(source_frame, text="Load", style="Accent.TButton", command=self.refresh_data)
        self.load_button.grid(row=0, column=1, sticky="e")

        paned = ttk.PanedWindow(main, orient=tk.HORIZONTAL)
        paned.grid(row=2, column=0, sticky="nsew")

        sidebar = ttk.Frame(paned, padding=(0, 0, 10, 0))
        results = ttk.Frame(paned)
        paned.add(sidebar, weight=0)
        paned.add(results, weight=1)

        self._build_sidebar(sidebar)
        self._build_results(results)

        status = ttk.Label(main, textvariable=self.status_var, style="Muted.TLabel", anchor="w")
        status.grid(row=3, column=0, sticky="ew", pady=(8, 0))

    def _build_sidebar(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        cars_frame = ttk.LabelFrame(parent, text="Cars", padding=10)
        cars_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 10))
        cars_frame.columnconfigure(0, weight=1)
        ttk.Entry(cars_frame, textvariable=self.car_search_var).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        car_list_frame = ttk.Frame(cars_frame)
        car_list_frame.grid(row=1, column=0, columnspan=2, sticky="nsew")
        car_list_frame.columnconfigure(0, weight=1)
        car_list_frame.rowconfigure(0, weight=1)
        self.car_listbox = tk.Listbox(
            car_list_frame,
            selectmode=tk.EXTENDED,
            height=12,
            exportselection=False,
            activestyle="dotbox",
            bg="#fffdf8",
            fg="#263238",
            selectbackground="#2f5d62",
            selectforeground="#ffffff",
            relief=tk.FLAT,
        )
        car_scrollbar = ttk.Scrollbar(car_list_frame, orient=tk.VERTICAL, command=self.car_listbox.yview)
        self.car_listbox.configure(yscrollcommand=car_scrollbar.set)
        self.car_listbox.grid(row=0, column=0, sticky="nsew")
        car_scrollbar.grid(row=0, column=1, sticky="ns")
        self.car_listbox.bind("<<ListboxSelect>>", self._on_car_selection)

        ttk.Button(cars_frame, text="Select visible", command=self.select_visible_cars).grid(row=2, column=0, sticky="ew", pady=(8, 0), padx=(0, 4))
        ttk.Button(cars_frame, text="Clear", command=self.clear_selected_cars).grid(row=2, column=1, sticky="ew", pady=(8, 0), padx=(4, 0))

        filters_frame = ttk.LabelFrame(parent, text="Filters", padding=10)
        filters_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 10))
        filters_frame.columnconfigure(1, weight=1)

        filter_row = 0
        self.common_filter_boxes = {}
        for column in COMMON_FILTER_COLUMNS:
            ttk.Label(filters_frame, text=column).grid(row=filter_row, column=0, sticky="w", pady=2)
            combo = ttk.Combobox(filters_frame, textvariable=self.common_filter_vars[column], state="readonly", values=("All",), width=22)
            combo.grid(row=filter_row, column=1, sticky="ew", pady=2)
            combo.bind("<<ComboboxSelected>>", lambda _event: self.apply_filters())
            self.common_filter_boxes[column] = combo
            filter_row += 1

        ttk.Separator(filters_frame).grid(row=filter_row, column=0, columnspan=2, sticky="ew", pady=8)
        filter_row += 1

        ttk.Label(filters_frame, text="Contains").grid(row=filter_row, column=0, sticky="w", pady=2)
        self.text_filter_column_box = ttk.Combobox(filters_frame, textvariable=self.text_filter_column_var, state="readonly", width=18)
        self.text_filter_column_box.grid(row=filter_row, column=1, sticky="ew", pady=2)
        filter_row += 1
        ttk.Entry(filters_frame, textvariable=self.text_filter_value_var).grid(row=filter_row, column=0, columnspan=2, sticky="ew", pady=2)
        filter_row += 1
        ttk.Button(filters_frame, text="Add text filter", command=self.add_text_filter).grid(row=filter_row, column=0, columnspan=2, sticky="ew", pady=(4, 8))
        filter_row += 1

        self.text_filter_listbox = tk.Listbox(filters_frame, height=3, exportselection=False, bg="#fffdf8", relief=tk.FLAT)
        self.text_filter_listbox.grid(row=filter_row, column=0, columnspan=2, sticky="ew")
        filter_row += 1
        ttk.Button(filters_frame, text="Remove selected text filter", command=self.remove_text_filter).grid(row=filter_row, column=0, columnspan=2, sticky="ew", pady=(4, 8))
        filter_row += 1

        ttk.Label(filters_frame, text="Test range").grid(row=filter_row, column=0, sticky="w", pady=2)
        self.range_column_box = ttk.Combobox(filters_frame, textvariable=self.range_column_var, state="readonly", width=18)
        self.range_column_box.grid(row=filter_row, column=1, sticky="ew", pady=2)
        filter_row += 1
        range_inputs = ttk.Frame(filters_frame)
        range_inputs.grid(row=filter_row, column=0, columnspan=2, sticky="ew")
        range_inputs.columnconfigure(0, weight=1)
        range_inputs.columnconfigure(1, weight=1)
        ttk.Entry(range_inputs, textvariable=self.range_min_var).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Entry(range_inputs, textvariable=self.range_max_var).grid(row=0, column=1, sticky="ew", padx=(4, 0))
        filter_row += 1
        ttk.Button(filters_frame, text="Add numeric range", command=self.add_range_filter).grid(row=filter_row, column=0, columnspan=2, sticky="ew", pady=(4, 8))
        filter_row += 1

        self.range_filter_listbox = tk.Listbox(filters_frame, height=3, exportselection=False, bg="#fffdf8", relief=tk.FLAT)
        self.range_filter_listbox.grid(row=filter_row, column=0, columnspan=2, sticky="ew")
        filter_row += 1
        ttk.Button(filters_frame, text="Remove selected range", command=self.remove_range_filter).grid(row=filter_row, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        columns_frame = ttk.LabelFrame(parent, text="Shown columns", padding=10)
        columns_frame.grid(row=2, column=0, sticky="nsew")
        columns_frame.columnconfigure(0, weight=1)
        columns_frame.rowconfigure(0, weight=1)
        self.column_listbox = tk.Listbox(
            columns_frame,
            selectmode=tk.EXTENDED,
            height=8,
            exportselection=False,
            bg="#fffdf8",
            fg="#263238",
            selectbackground="#2f5d62",
            selectforeground="#ffffff",
            relief=tk.FLAT,
        )
        column_scrollbar = ttk.Scrollbar(columns_frame, orient=tk.VERTICAL, command=self.column_listbox.yview)
        self.column_listbox.configure(yscrollcommand=column_scrollbar.set)
        self.column_listbox.grid(row=0, column=0, sticky="nsew")
        column_scrollbar.grid(row=0, column=1, sticky="ns")
        self.column_listbox.bind("<<ListboxSelect>>", lambda _event: self.apply_filters())

    def _build_results(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        toolbar = ttk.LabelFrame(parent, text="Results", padding=10)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        toolbar.columnconfigure(1, weight=1)

        ttk.Label(toolbar, text="Sort by").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.sort_box = ttk.Combobox(toolbar, textvariable=self.sort_var, state="readonly", width=22)
        self.sort_box.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.sort_box.bind("<<ComboboxSelected>>", lambda _event: self.apply_filters())
        ttk.Checkbutton(toolbar, text="Descending", variable=self.desc_var, command=self.apply_filters).grid(row=0, column=2, padx=(0, 8))
        ttk.Label(toolbar, text="Limit").grid(row=0, column=3, sticky="e", padx=(0, 6))
        limit_spinner = tk.Spinbox(toolbar, textvariable=self.limit_var, from_=0, to=10000, width=7, command=self.apply_filters)
        limit_spinner.grid(row=0, column=4, sticky="e", padx=(0, 8))
        limit_spinner.bind("<Return>", lambda _event: self.apply_filters())
        ttk.Button(toolbar, text="Apply", style="Accent.TButton", command=self.apply_filters).grid(row=0, column=5, sticky="e")

        export_bar = ttk.Frame(toolbar)
        export_bar.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(8, 0))
        ttk.Button(export_bar, text="Copy CSV", command=lambda: self.copy_output("csv")).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(export_bar, text="Copy Markdown", command=lambda: self.copy_output("markdown")).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(export_bar, text="Save CSV", command=self.save_csv).grid(row=0, column=2)

        table_frame = ttk.Frame(parent)
        table_frame.grid(row=1, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.results_table = ttk.Treeview(table_frame, show="headings")
        y_scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.results_table.yview)
        x_scrollbar = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.results_table.xview)
        self.results_table.configure(yscrollcommand=y_scrollbar.set, xscrollcommand=x_scrollbar.set)
        self.results_table.grid(row=0, column=0, sticky="nsew")
        y_scrollbar.grid(row=0, column=1, sticky="ns")
        x_scrollbar.grid(row=1, column=0, sticky="ew")

    def refresh_data(self):
        source = self.source_var.get().strip() or DEFAULT_SOURCE
        self.load_button.configure(state=tk.DISABLED)
        self.status_var.set("Loading spreadsheet...")

        worker = threading.Thread(target=self._load_rows_in_background, args=(source,), daemon=True)
        worker.start()

    def _load_rows_in_background(self, source):
        try:
            headers, rows = load_rows(source)
            self.root.after(0, lambda: self._finish_load(headers, rows, None))
        except (csv.Error, FileNotFoundError, RuntimeError, urllib.error.URLError, ValueError) as error:
            self.root.after(0, lambda load_error=error: self._finish_load([], [], load_error))

    def _finish_load(self, headers, rows, error):
        self.load_button.configure(state=tk.NORMAL)
        if error is not None:
            self.status_var.set(f"Could not load spreadsheet: {error}")
            messagebox.showerror("Load failed", str(error))
            return

        self.headers = headers
        self.rows = rows
        self.selected_cars.clear()
        self.text_filters.clear()
        self.range_filters.clear()
        self._update_filter_lists()
        self._update_available_values()
        self._update_car_names()
        self._update_column_options()
        self.apply_filters()

    def _update_available_values(self):
        for column, combo in self.common_filter_boxes.items():
            if column in self.headers:
                values = ["All", *self._unique_values(column)]
                combo.configure(values=values, state="readonly")
                self.common_filter_vars[column].set("All")
            else:
                combo.configure(values=("All",), state=tk.DISABLED)
                self.common_filter_vars[column].set("All")

        self.text_filter_column_box.configure(values=self.headers)
        if self.headers:
            self.text_filter_column_var.set(self.headers[0])

        numeric_columns = self._numeric_columns()
        self.range_column_box.configure(values=numeric_columns)
        self.range_column_var.set(numeric_columns[0] if numeric_columns else "")
        self.sort_box.configure(values=["", *self.headers])
        self.sort_var.set("Average" if "Average" in self.headers else "")

    def _unique_values(self, column):
        values = {row.get(column, "") for row in self.rows if row.get(column, "")}
        return sorted(values, key=str.casefold)

    def _numeric_columns(self):
        columns = []
        for column in self.headers:
            values = [row.get(column, "") for row in self.rows if row.get(column, "")]
            if values and any(numeric_value(value) is not None for value in values):
                columns.append(column)
        return columns

    def _update_car_names(self):
        if "Car" not in self.headers:
            self.all_car_names = []
        else:
            self.all_car_names = sorted({row.get("Car", "") for row in self.rows if row.get("Car", "")}, key=str.casefold)
        self._update_car_list()

    def _update_car_list(self):
        if not hasattr(self, "car_listbox"):
            return

        query = self.car_search_var.get().strip().casefold()
        self.visible_car_names = [name for name in self.all_car_names if not query or query in name.casefold()]

        self.updating_car_list = True
        self.car_listbox.delete(0, tk.END)
        for name in self.visible_car_names:
            self.car_listbox.insert(tk.END, name)
        for index, name in enumerate(self.visible_car_names):
            if name in self.selected_cars:
                self.car_listbox.selection_set(index)
        self.updating_car_list = False

    def _update_column_options(self):
        self.column_listbox.delete(0, tk.END)
        for column in self.headers:
            self.column_listbox.insert(tk.END, column)

        default_selection = display_columns(self.headers, None)

        for index, column in enumerate(self.headers):
            if column in default_selection:
                self.column_listbox.selection_set(index)

    def _update_filter_lists(self):
        self.text_filter_listbox.delete(0, tk.END)
        for column, value in self.text_filters:
            self.text_filter_listbox.insert(tk.END, f"{column} contains {value}")

        self.range_filter_listbox.delete(0, tk.END)
        for column, minimum, maximum in self.range_filters:
            lower_text = minimum if minimum else "any"
            upper_text = maximum if maximum else "any"
            self.range_filter_listbox.insert(tk.END, f"{column}: {lower_text} to {upper_text}")

    def _on_car_selection(self, _event):
        if self.updating_car_list:
            return

        visible_names = set(self.visible_car_names)
        selected_visible = {self.visible_car_names[index] for index in self.car_listbox.curselection()}
        self.selected_cars.difference_update(visible_names)
        self.selected_cars.update(selected_visible)
        self.apply_filters()

    def select_visible_cars(self):
        self.selected_cars.update(self.visible_car_names)
        self._update_car_list()
        self.apply_filters()

    def clear_selected_cars(self):
        self.selected_cars.clear()
        self._update_car_list()
        self.apply_filters()

    def add_text_filter(self):
        column = self.text_filter_column_var.get().strip()
        value = self.text_filter_value_var.get().strip()
        if not column or not value:
            messagebox.showwarning("Missing filter", "Choose a column and enter text to filter by.")
            return

        self.text_filters.append((column, value))
        self.text_filter_value_var.set("")
        self._update_filter_lists()
        self.apply_filters()

    def remove_text_filter(self):
        selected = list(self.text_filter_listbox.curselection())
        for index in reversed(selected):
            del self.text_filters[index]
        self._update_filter_lists()
        self.apply_filters()

    def add_range_filter(self):
        column = self.range_column_var.get().strip()
        minimum = self.range_min_var.get().strip()
        maximum = self.range_max_var.get().strip()
        if not column or (not minimum and not maximum):
            messagebox.showwarning("Missing range", "Choose a test column and enter a minimum, maximum, or both.")
            return
        if minimum and numeric_value(minimum) is None:
            messagebox.showwarning("Invalid minimum", "The minimum value must be numeric.")
            return
        if maximum and numeric_value(maximum) is None:
            messagebox.showwarning("Invalid maximum", "The maximum value must be numeric.")
            return

        self.range_filters.append((column, minimum, maximum))
        self.range_min_var.set("")
        self.range_max_var.set("")
        self._update_filter_lists()
        self.apply_filters()

    def remove_range_filter(self):
        selected = list(self.range_filter_listbox.curselection())
        for index in reversed(selected):
            del self.range_filters[index]
        self._update_filter_lists()
        self.apply_filters()

    def apply_filters(self):
        if not self.headers:
            return

        try:
            rows = list(self.rows)
            if self.selected_cars and "Car" in self.headers:
                rows = [row for row in rows if row.get("Car", "") in self.selected_cars]

            text_filter_expressions = []
            for column, variable in self.common_filter_vars.items():
                value = variable.get()
                if column in self.headers and value and value != "All":
                    text_filter_expressions.append(f"{column}={value}")
            text_filter_expressions.extend(f"{column}={value}" for column, value in self.text_filters)
            rows = apply_text_filters(rows, self.headers, text_filter_expressions)

            minimums = []
            maximums = []
            for column, minimum, maximum in self.range_filters:
                if minimum:
                    minimums.append(f"{column}={minimum}")
                if maximum:
                    maximums.append(f"{column}={maximum}")
            rows = apply_numeric_filters(rows, self.headers, minimums, maximums)

            rows = sort_rows(rows, self.headers, self.sort_var.get().strip(), self.desc_var.get())
            total_count = len(rows)
            limit = int(self.limit_var.get() or "0")
            if limit > 0:
                rows = rows[:limit]

            columns = self._selected_columns()
            self.filtered_rows = rows
            self.current_columns = columns
            self._render_table(rows, columns)
            selected_text = f", {len(self.selected_cars)} selected cars" if self.selected_cars else ""
            self.status_var.set(f"{total_count} matching rows, showing {len(rows)}{selected_text}.")
        except (ValueError, csv.Error) as error:
            self.status_var.set(f"Filter error: {error}")
            messagebox.showerror("Filter error", str(error))

    def _selected_columns(self):
        selected = [self.headers[index] for index in self.column_listbox.curselection()]
        if selected:
            return selected
        return display_columns(self.headers, None)

    def _render_table(self, rows, columns):
        self.results_table.delete(*self.results_table.get_children())
        self.results_table.configure(columns=columns)

        for column in columns:
            values = [row.get(column, "") for row in rows[:80]]
            width = max(90, min(260, max([len(column), *[len(value) for value in values]]) * 8 + 24))
            self.results_table.heading(column, text=column)
            self.results_table.column(column, width=width, minwidth=80, stretch=True, anchor=tk.W)

        for row in rows:
            self.results_table.insert("", tk.END, values=[row.get(column, "") for column in columns])

    def copy_output(self, output_format):
        if not self.current_columns:
            return
        if output_format == "markdown":
            text = markdown_output(self.filtered_rows, self.current_columns)
        else:
            text = csv_output(self.filtered_rows, self.current_columns)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_var.set(f"Copied {output_format.upper()} for {len(self.filtered_rows)} rows.")

    def save_csv(self):
        if not self.current_columns:
            return
        path = filedialog.asksaveasfilename(
            title="Save results as CSV",
            defaultextension=".csv",
            filetypes=(("CSV files", "*.csv"), ("All files", "*.*")),
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8", newline="") as csv_file:
            csv_file.write(csv_output(self.filtered_rows, self.current_columns))
            csv_file.write("\n")
        self.status_var.set(f"Saved {len(self.filtered_rows)} rows to {path}.")


def launch_gui(initial_source=DEFAULT_SOURCE):
    if tk is None:
        raise RuntimeError("Tkinter is not available in this Python installation.")

    root = tk.Tk()
    CarCompareApp(root, initial_source)
    root.mainloop()


def build_parser():
    parser = argparse.ArgumentParser(
        description="Fetch a car spreadsheet and compare rows with filters, sorting, and selected columns.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    %(prog)s --web
    %(prog)s --build-static docs
  %(prog)s --filter Season=Summer --filter Surface=Dry --sort Average --limit 10
  %(prog)s --compare "Volvo ES90" "Nio EL8" --columns Car,Tires,Average,"100 km/h"
  %(prog)s --min "Average=63" --max "Average=65" --sort Average --output markdown
  %(prog)s --source cars.csv --filter Tires=Michelin
""",
    )
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="Google Sheets URL, CSV export URL, or local CSV path.")
    parser.add_argument("--web", action="store_true", help="Open the local website comparer.")
    parser.add_argument("--gui", action="store_true", help="Open the desktop GUI.")
    parser.add_argument("--host", default="127.0.0.1", help="Host for --web. Default: 127.0.0.1.")
    parser.add_argument("--port", type=int, default=8765, help="Starting port for --web. Default: 8765.")
    parser.add_argument("--no-browser", action="store_true", help="Start --web without opening a browser.")
    parser.add_argument("--export-workbook", metavar="PATH", help="Export all spreadsheet tabs to a static workbook JSON file.")
    parser.add_argument("--build-static", metavar="DIR", help="Build a static GitHub Pages site into DIR.")
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
    if len(sys.argv) == 1:
        try:
            launch_web()
            return 0
        except RuntimeError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.build_static:
            index_path, workbook_path, workbook = build_static_site(args.source, args.build_static)
            summary = workbook["summary"]
            print(f"Built static site: {index_path}")
            print(f"Exported workbook: {workbook_path}")
            print(f"Workbook: {summary['loadedSheetCount']}/{summary['sheetCount']} tabs, {summary['rowCount']} rows")
            return 0
        if args.export_workbook:
            workbook = export_workbook(args.source, args.export_workbook)
            summary = workbook["summary"]
            print(f"Exported workbook: {args.export_workbook}")
            print(f"Workbook: {summary['loadedSheetCount']}/{summary['sheetCount']} tabs, {summary['rowCount']} rows")
            return 0
        if args.web:
            launch_web(args.source, args.host, args.port, not args.no_browser)
            return 0
        if args.gui:
            launch_gui(args.source)
            return 0
        return run(args)
    except (csv.Error, FileNotFoundError, OSError, RuntimeError, urllib.error.URLError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

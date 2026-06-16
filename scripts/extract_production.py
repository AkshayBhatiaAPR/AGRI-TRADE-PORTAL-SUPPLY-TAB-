#!/usr/bin/env python3
"""
extract_production.py — UPAg APY Data Extractor

Uses Playwright to access the UPAg portal (upag.gov.in), downloads the
All India Year-wise Crop APY CSV, extracts Area, Production, and Yield
for six commodities (Paddy, Wheat, Maize, Sugarcane, Tur, Gram), and
writes a combined production.json file.

Usage:
    python scripts/extract_production.py

Dependencies:
    pip install playwright
    playwright install chromium --with-deps
"""

import csv
import io
import json
import os
import sys
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("extract_production")

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

UPAG_URL = (
    "https://upag.gov.in/dash-reports/allindiaapyyearwise"
    "?rtab=Area%2C+Production+%26+Yield&rtype=reports"
)

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)
PRODUCTION_JSON = os.path.join(DATA_DIR, "production.json")
MANIFEST_PATH = os.path.join(DATA_DIR, "manifest.json")

# Crop name mapping: UPAg column name → our canonical key
CROP_MAP = {
    "rice": "paddy",
    "paddy": "paddy",
    "wheat": "wheat",
    "maize": "maize",
    "sugarcane": "sugarcane",
    "tur": "tur",
    "tur (arhar)": "tur",
    "arhar": "tur",
    "gram": "gram",
}

CANONICAL = ["paddy", "wheat", "maize", "sugarcane", "tur", "gram"]
DISPLAY = {
    "paddy": "Paddy", "wheat": "Wheat", "maize": "Maize",
    "sugarcane": "Sugarcane", "tur": "Tur", "gram": "Gram",
}
VALID_SEASONS = {"kharif", "rabi", "summer", "total"}


# ---------------------------------------------------------------------------
# PHASE 1: DOWNLOAD CSV VIA PLAYWRIGHT
# ---------------------------------------------------------------------------

def download_csv_from_upag():
    """
    Use Playwright headless browser to:
    1. Navigate to UPAg APY page
    2. Wait for table to render
    3. Click CSV download button
    4. Return the downloaded file content
    """
    from playwright.sync_api import sync_playwright

    log.info(f"Launching headless browser...")
    log.info(f"Target URL: {UPAG_URL}")

    csv_content = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        # Navigate to UPAg
        log.info("Navigating to UPAg...")
        page.goto(UPAG_URL, wait_until="networkidle", timeout=120000)

        # Wait for the table to load
        log.info("Waiting for table to render...")
        try:
            page.wait_for_selector("table", timeout=60000)
            log.info("Table detected")
        except Exception:
            # Try waiting for any data content
            page.wait_for_timeout(15000)
            log.info("Waited 15s for content")

        # Look for the CSV download button and click it
        log.info("Looking for CSV download button...")

        # Try multiple selectors for the CSV button
        csv_selectors = [
            "button:has-text('CSV')",
            "a:has-text('CSV')",
            "button:has-text('csv')",
            "[title*='CSV']",
            "[aria-label*='CSV']",
            ".csv-btn",
            "button >> text=CSV",
        ]

        download_clicked = False
        for selector in csv_selectors:
            try:
                element = page.query_selector(selector)
                if element and element.is_visible():
                    log.info(f"Found CSV button with selector: {selector}")

                    # Start waiting for download before clicking
                    with page.expect_download(timeout=60000) as download_info:
                        element.click()

                    download = download_info.value
                    log.info(f"Download started: {download.suggested_filename}")

                    # Save to temp path and read content
                    tmp_path = download.path()
                    if tmp_path:
                        with open(tmp_path, "r", encoding="utf-8", errors="replace") as f:
                            csv_content = f.read()
                        log.info(f"Downloaded {len(csv_content)} chars")
                    download_clicked = True
                    break
            except Exception as e:
                continue

        # Fallback: try EXCEL button if CSV didn't work
        if not download_clicked:
            log.info("CSV button not found, trying EXCEL button...")
            excel_selectors = [
                "button:has-text('EXCEL')",
                "a:has-text('EXCEL')",
                "button:has-text('Excel')",
            ]
            for selector in excel_selectors:
                try:
                    element = page.query_selector(selector)
                    if element and element.is_visible():
                        log.info(f"Found EXCEL button with selector: {selector}")
                        with page.expect_download(timeout=60000) as download_info:
                            element.click()
                        download = download_info.value
                        log.info(f"Download started: {download.suggested_filename}")
                        tmp_path = download.path()
                        if tmp_path:
                            # Read as binary for Excel
                            with open(tmp_path, "rb") as f:
                                excel_bytes = f.read()
                            csv_content = convert_excel_to_csv(excel_bytes)
                            log.info(f"Converted Excel to CSV: {len(csv_content)} chars")
                        download_clicked = True
                        break
                except Exception:
                    continue

        # Last resort: scrape the table directly from HTML
        if not download_clicked or not csv_content:
            log.info("Download buttons failed, scraping table from HTML...")
            csv_content = scrape_table_from_page(page)

        browser.close()

    if not csv_content:
        log.error("Failed to get data from UPAg")
        return None

    return csv_content


def convert_excel_to_csv(excel_bytes):
    """Convert Excel bytes to CSV string using openpyxl."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(excel_bytes), data_only=True)
        ws = wb.active
        output = io.StringIO()
        writer = csv.writer(output)
        for row in ws.iter_rows(values_only=True):
            writer.writerow(row)
        return output.getvalue()
    except ImportError:
        log.error("openpyxl not installed — cannot convert Excel")
        return None


def scrape_table_from_page(page):
    """Scrape the rendered HTML table directly."""
    try:
        # Get all table rows
        rows = page.query_selector_all("table tr")
        if not rows:
            log.error("No table rows found")
            return None

        output = io.StringIO()
        writer = csv.writer(output)

        for row in rows:
            cells = row.query_selector_all("th, td")
            values = [cell.inner_text().strip() for cell in cells]
            if any(v for v in values):
                writer.writerow(values)

        result = output.getvalue()
        log.info(f"Scraped {len(rows)} rows from HTML table")
        return result
    except Exception as e:
        log.error(f"Table scraping failed: {e}")
        return None


# ---------------------------------------------------------------------------
# PHASE 2: PARSE CSV
# ---------------------------------------------------------------------------

def normalize_crop(name):
    """Map a column/crop name to canonical key."""
    if not name:
        return None
    clean = name.strip().lower()
    clean = re.sub(r"\s+", " ", clean)
    if clean in CROP_MAP:
        return CROP_MAP[clean]
    for alias, canonical in CROP_MAP.items():
        if alias in clean:
            return canonical
    return None


def normalize_season(s):
    """Normalize season text."""
    if not s:
        return None
    s = s.strip().lower()
    if "kharif" in s: return "kharif"
    if "rabi" in s: return "rabi"
    if "summer" in s: return "summer"
    if "total" in s: return "total"
    return None


def parse_number(val):
    """Parse numeric value, return float or None."""
    if val is None:
        return None
    val = str(val).strip()
    if not val or val in ("", "-", "--", "NA", "N/A", "null", "None"):
        return None
    val = val.replace(",", "")
    try:
        return float(val)
    except ValueError:
        return None


def parse_crop_year(text):
    """Extract year pattern like 2024-25."""
    m = re.search(r"(\d{4})-(\d{2,4})", str(text))
    if m:
        start, end = m.group(1), m.group(2)
        if len(end) == 4:
            end = end[2:]
        return f"{start}-{end}"
    return None


def detect_csv_format(lines):
    """
    Detect whether the CSV is in wide format (crops as column groups)
    or long format (one row per crop-season-year).
    Returns 'wide' or 'long'.
    """
    if not lines:
        return None
    header = lines[0].lower()
    # Wide format: Year, Season, Rice_Area, Rice_Production, Rice_Yield, Wheat_Area, ...
    # Or: Year, Season, [Rice] Area, Production, Yield, [Wheat] Area, ...
    if "year" in header and ("rice" in header or "wheat" in header or "maize" in header):
        return "wide"
    # Long format: Year, Season, Crop, Area, Production, Yield
    if "crop" in header and "area" in header:
        return "long"
    # If header has Area, Production, Yield repeated multiple times → wide
    if header.count("area") > 1 or header.count("product") > 1:
        return "wide"
    return "wide"  # default


def parse_csv_data(csv_content):
    """
    Parse the CSV/scraped data and return structured dict.
    Returns: {commodity: {year: {season: {area, production, yield}}}}
    """
    extracted = {c: {} for c in CANONICAL}

    lines = csv_content.strip().split("\n")
    if len(lines) < 2:
        log.error("CSV has fewer than 2 lines")
        return extracted

    reader = csv.reader(io.StringIO(csv_content))
    rows = list(reader)

    if len(rows) < 2:
        log.error("CSV has fewer than 2 rows")
        return extracted

    # Find the header row (might not be row 0 — could have title rows)
    header_idx = 0
    header = rows[0]

    # Check if first row is a title/metadata row
    for i, row in enumerate(rows[:5]):
        row_text = " ".join(str(c) for c in row).lower()
        if "year" in row_text and "season" in row_text:
            header_idx = i
            header = row
            break
        # Also check for crop names in header (wide format with 2-level header)
        crop_matches = sum(1 for c in row if normalize_crop(str(c)) is not None)
        if crop_matches >= 2:
            header_idx = i
            header = row
            break

    log.info(f"Header at row {header_idx}: {header[:8]}...")

    # Detect column structure
    # The UPAg table has: Year | Season | [Crop1] Area | Prod | Yield | [Crop2] Area | Prod | Yield | ...
    # Or could be: Year | Season | Rice_Area | Rice_Production | Rice_Yield | Wheat_Area | ...

    # Build column mapping: find which columns belong to which crop
    crop_columns = {}  # {canonical_key: {area_col, prod_col, yield_col}}

    # Check if there's a secondary header row (crop names above Area/Prod/Yield)
    if header_idx > 0:
        crop_header = rows[header_idx - 1]
    else:
        crop_header = None

    # Strategy 1: Check for multi-level headers (crop names in row above)
    if crop_header:
        current_crop = None
        for ci, cell in enumerate(crop_header):
            crop = normalize_crop(str(cell))
            if crop:
                current_crop = crop
            if current_crop and ci < len(header):
                col_name = str(header[ci]).strip().lower()
                if current_crop not in crop_columns:
                    crop_columns[current_crop] = {}
                if "area" in col_name:
                    crop_columns[current_crop]["area"] = ci
                elif "prod" in col_name:
                    crop_columns[current_crop]["prod"] = ci
                elif "yield" in col_name:
                    crop_columns[current_crop]["yield"] = ci

    # Strategy 2: Check for combined column names like "Rice Area", "Rice Production"
    if not crop_columns:
        for ci, col_name in enumerate(header):
            col_lower = str(col_name).strip().lower()
            for alias, canonical in CROP_MAP.items():
                if alias in col_lower:
                    if canonical not in crop_columns:
                        crop_columns[canonical] = {}
                    if "area" in col_lower:
                        crop_columns[canonical]["area"] = ci
                    elif "prod" in col_lower:
                        crop_columns[canonical]["prod"] = ci
                    elif "yield" in col_lower:
                        crop_columns[canonical]["yield"] = ci
                    break

    # Strategy 3: Positional — UPAg format: Year, Season, then groups of 3 (Area, Prod, Yield)
    if not crop_columns:
        log.info("Using positional column detection...")
        # Find Year and Season columns
        year_col = None
        season_col = None
        for ci, col in enumerate(header):
            cl = str(col).strip().lower()
            if "year" in cl:
                year_col = ci
            elif "season" in cl:
                season_col = ci

        if year_col is not None and season_col is not None:
            # After Year and Season, look for repeating Area/Prod/Yield pattern
            data_start = max(year_col, season_col) + 1

            # Try to detect crop names from the row ABOVE header or from context
            # From screenshot: crops appear in a merged header row above
            if crop_header:
                current_crop = None
                col_idx = data_start
                while col_idx < len(header):
                    # Check crop header row for crop name
                    if col_idx < len(crop_header):
                        crop = normalize_crop(str(crop_header[col_idx]))
                        if crop:
                            current_crop = crop
                    if current_crop:
                        if current_crop not in crop_columns:
                            crop_columns[current_crop] = {}
                        col_lower = str(header[col_idx]).strip().lower()
                        if "area" in col_lower:
                            crop_columns[current_crop]["area"] = col_idx
                        elif "prod" in col_lower:
                            crop_columns[current_crop]["prod"] = col_idx
                        elif "yield" in col_lower:
                            crop_columns[current_crop]["yield"] = col_idx
                    col_idx += 1

    # Strategy 4: Long format (Year, Season, Crop, Area, Production, Yield)
    if not crop_columns:
        header_lower = [str(h).strip().lower() for h in header]
        if "crop" in header_lower:
            log.info("Detected long format CSV")
            crop_col = header_lower.index("crop")
            area_col = next((i for i, h in enumerate(header_lower) if "area" in h), None)
            prod_col = next((i for i, h in enumerate(header_lower) if "prod" in h), None)
            yield_col = next((i for i, h in enumerate(header_lower) if "yield" in h), None)
            yr_col = next((i for i, h in enumerate(header_lower) if "year" in h), None)
            ssn_col = next((i for i, h in enumerate(header_lower) if "season" in h), None)

            for row in rows[header_idx + 1:]:
                if len(row) <= max(c for c in [crop_col, yr_col, ssn_col] if c is not None):
                    continue
                crop = normalize_crop(str(row[crop_col])) if crop_col is not None else None
                if not crop:
                    continue
                year = parse_crop_year(str(row[yr_col])) if yr_col is not None else None
                season = normalize_season(str(row[ssn_col])) if ssn_col is not None else None
                if not year or not season:
                    continue

                area = parse_number(row[area_col]) if area_col is not None and area_col < len(row) else None
                prod = parse_number(row[prod_col]) if prod_col is not None and prod_col < len(row) else None
                yld = parse_number(row[yield_col]) if yield_col is not None and yield_col < len(row) else None

                if year not in extracted[crop]:
                    extracted[crop][year] = {}
                extracted[crop][year][season] = {
                    "area": area, "production": prod, "yield": yld
                }

            return extracted

    if not crop_columns:
        log.error("Could not detect column structure")
        log.info(f"Header: {header}")
        if crop_header:
            log.info(f"Crop header: {crop_header}")
        return extracted

    log.info(f"Detected {len(crop_columns)} crops: {list(crop_columns.keys())}")
    for crop, cols in crop_columns.items():
        log.info(f"  {DISPLAY.get(crop, crop)}: {cols}")

    # Find year and season columns
    year_col = None
    season_col = None
    header_lower = [str(h).strip().lower() for h in header]
    for ci, cl in enumerate(header_lower):
        if "year" in cl:
            year_col = ci
        elif "season" in cl:
            season_col = ci

    if year_col is None or season_col is None:
        log.error(f"Year/Season columns not found in header: {header[:5]}")
        return extracted

    # Parse data rows
    for row in rows[header_idx + 1:]:
        if not row or len(row) <= season_col:
            continue

        year = parse_crop_year(str(row[year_col]))
        season = normalize_season(str(row[season_col]))

        if not year or not season:
            continue

        for crop, cols in crop_columns.items():
            area = parse_number(row[cols["area"]]) if "area" in cols and cols["area"] < len(row) else None
            prod = parse_number(row[cols["prod"]]) if "prod" in cols and cols["prod"] < len(row) else None
            yld = parse_number(row[cols["yield"]]) if "yield" in cols and cols["yield"] < len(row) else None

            if area is None and prod is None and yld is None:
                continue

            if year not in extracted[crop]:
                extracted[crop][year] = {}
            extracted[crop][year][season] = {
                "area": area, "production": prod, "yield": yld
            }

    return extracted


# ---------------------------------------------------------------------------
# PHASE 3: WRITE JSON
# ---------------------------------------------------------------------------

def build_production_json(extracted):
    """Build the final production.json structure."""
    now = datetime.now(timezone.utc).isoformat()

    pj = {
        "meta": {
            "unit_area": "Lakh Hectares",
            "unit_production": "Lakh Tonnes",
            "unit_yield": "Kg/Hectare",
            "source": "DA&FW via UPAg (upag.gov.in)",
            "last_updated": now,
        },
        "commodities": {}
    }

    for commodity in CANONICAL:
        years_data = extracted.get(commodity, {})
        data_list = []

        for year_str in sorted(years_data.keys(), reverse=True):
            seasons = years_data[year_str]
            entry = {"year": year_str}
            for s in VALID_SEASONS:
                if s in seasons:
                    entry[s] = seasons[s]
                else:
                    entry[s] = None
            data_list.append(entry)

        pj["commodities"][commodity] = {
            "name": DISPLAY[commodity],
            "data": data_list,
        }

    return pj


def save_json(data, path):
    """Write JSON file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def save_manifest():
    """Update manifest.json."""
    now = datetime.now(timezone.utc).isoformat()
    manifest_path = MANIFEST_PATH

    if os.path.exists(manifest_path):
        with open(manifest_path) as f:
            manifest = json.load(f)
    else:
        manifest = {"processed": []}

    manifest["last_run"] = now
    manifest["processed"].append({
        "source": "UPAg CSV download",
        "processed_at": now,
        "url": UPAG_URL,
    })

    save_json(manifest, manifest_path)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    log.info("=" * 60)
    log.info("UPAg APY Data Extractor — Starting")
    log.info("=" * 60)

    # Phase 1: Download
    csv_content = download_csv_from_upag()
    if not csv_content:
        log.error("Failed to download data from UPAg")
        sys.exit(1)

    # Save raw CSV for debugging
    raw_path = os.path.join(DATA_DIR, "raw_upag.csv")
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(raw_path, "w") as f:
        f.write(csv_content)
    log.info(f"Raw CSV saved to {raw_path}")

    # Phase 2: Parse
    extracted = parse_csv_data(csv_content)

    # Check results
    total = sum(len(v) for v in extracted.values())
    if total == 0:
        log.error("No data extracted from CSV")
        sys.exit(1)

    for commodity in CANONICAL:
        years = list(extracted[commodity].keys())
        if years:
            log.info(f"  {DISPLAY[commodity]}: {len(years)} years "
                     f"({min(years)} to {max(years)})")
        else:
            log.warning(f"  {DISPLAY[commodity]}: NO DATA")

    # Phase 3: Write
    pj = build_production_json(extracted)
    save_json(pj, PRODUCTION_JSON)
    log.info(f"Saved {PRODUCTION_JSON}")

    save_manifest()
    log.info(f"Saved {MANIFEST_PATH}")

    log.info("=" * 60)
    log.info("Done!")
    log.info("=" * 60)


if __name__ == "__main__":
    main()

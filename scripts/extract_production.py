#!/usr/bin/env python3
"""
extract_production.py — DA&FW Advance Estimates Production Extractor

Scrapes desagri.gov.in/statistics-type/advance-estimates/ for PDF links,
downloads unprocessed PDFs, extracts production data (Lakh Tonnes) for
six commodities (Paddy, Wheat, Maize, Sugarcane, Tur, Gram), and writes
commodity-wise JSON files.

Usage:
    python scripts/extract_production.py

Dependencies:
    pip install pdfplumber requests beautifulsoup4
"""

import json
import os
import re
import sys
import tempfile
import logging
from datetime import datetime, timezone

import requests
import pdfplumber
from bs4 import BeautifulSoup
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

SOURCE_URL = "https://desagri.gov.in/statistics-type/advance-estimates/"
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
MANIFEST_PATH = os.path.join(DATA_DIR, "manifest.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

CROP_ALIASES = {
    "rice": "paddy", "paddy": "paddy",
    "wheat": "wheat",
    "maize": "maize",
    "sugarcane": "sugarcane",
    "tur": "tur", "arhar": "tur", "tur(arhar)": "tur",
    "tur (arhar)": "tur", "arhar/tur": "tur", "arhar (tur)": "tur",
    "gram": "gram",
}

CANONICAL_COMMODITIES = ["paddy", "wheat", "maize", "sugarcane", "tur", "gram"]
COMMODITY_DISPLAY = {
    "paddy": "Paddy", "wheat": "Wheat", "maize": "Maize",
    "sugarcane": "Sugarcane", "tur": "Tur", "gram": "Gram",
}

VALID_SEASONS = {"kharif", "rabi", "summer", "total"}

ESTIMATE_MAP = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2,
    "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("extract_production")

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)

def load_manifest():
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r") as f:
            return json.load(f)
    return {"last_run": None, "processed": []}

def save_manifest(manifest):
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

def load_production_json():
    path = os.path.join(DATA_DIR, "production.json")
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    # Initialize empty structure
    structure = {
        "meta": {
            "unit": "Lakh Tonnes",
            "last_updated": None,
            "source_pdf": None,
            "estimate_type": None,
            "estimate_year": None,
        },
        "commodities": {}
    }
    for c in CANONICAL_COMMODITIES:
        structure["commodities"][c] = {
            "name": COMMODITY_DISPLAY[c],
            "data": []
        }
    return structure

def save_production_json(data):
    path = os.path.join(DATA_DIR, "production.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def parse_crop_year(text):
    m = re.search(r"(\d{4})-(\d{2,4})", text)
    if m:
        start, end = m.group(1), m.group(2)
        if len(end) == 4:
            end = end[2:]
        return f"{start}-{end}"
    return None

def parse_estimate_number(text):
    text_lower = text.lower()
    for key, num in ESTIMATE_MAP.items():
        if key in text_lower:
            return num
    return None

def parse_estimate_label(num):
    labels = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}
    return f"{labels.get(num, str(num))} Advance Estimate"

def parse_number(val):
    if val is None:
        return None
    val = str(val).strip()
    if val in ("", "--", "-", "—", "*", "N.A.", "NA", "n.a.", "..", "@", "$", "#"):
        return None
    val = val.replace(",", "").replace(" ", "")
    val = re.sub(r"[*#@$`^]+", "", val)
    val = val.strip()
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None

def normalize_crop_name(name):
    if not name:
        return None
    clean = re.sub(r"\s+", " ", name.strip().lower())
    clean = re.sub(r"^\d+[\.\)]*\s*", "", clean).strip()
    if re.match(r"^\(?\d+\s*=", clean):
        return None
    if clean in CROP_ALIASES:
        return CROP_ALIASES[clean]
    for alias, canonical in CROP_ALIASES.items():
        if alias in clean:
            return canonical
    return None

def normalize_season(text):
    if not text:
        return None
    clean = text.strip().lower()
    if "kharif" in clean: return "kharif"
    if "rabi" in clean: return "rabi"
    if "summer" in clean: return "summer"
    if "total" in clean: return "total"
    return None

def is_aggregate_row(text):
    """Check if a row is an aggregate/header like Total Foodgrains, Cereals, etc."""
    if not text:
        return False
    lower = text.strip().lower()
    skip_keywords = [
        "cereal", "foodgrain", "food grain", "pulse", "oilseed", "oil seed",
        "commercial", "fibre", "plantation", "condiment", "nutri",
        "coarse", "shree anna", "total nine", "total five",
        "cotton", "jute", "mesta", "groundnut", "soyabean", "soybean",
        "sunflower", "sesamum", "rapeseed", "mustard", "linseed",
        "castor", "niger", "safflower", "tobacco",
    ]
    for kw in skip_keywords:
        if kw in lower:
            return True
    if re.match(r"^\(?\d+\s*=", lower):
        return True
    return False

# ---------------------------------------------------------------------------
# PHASE 1: DISCOVER
# ---------------------------------------------------------------------------

def discover_pdfs():
    log.info(f"Fetching index page: {SOURCE_URL}")
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(SOURCE_URL, headers=headers, timeout=60, verify=False)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    pdfs = []

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if not href.lower().endswith(".pdf"):
            continue
        link_text = a_tag.get_text(strip=True).lower()
        if "hindi" in link_text or "hindi" in href.lower():
            continue
        if href.startswith("/"):
            href = "https://desagri.gov.in" + href
        elif not href.startswith("http"):
            continue

        filename = href.split("/")[-1]

        title_text = ""
        parent = a_tag
        for _ in range(10):
            parent = parent.parent
            if parent is None:
                break
            parent_text = parent.get_text(" ", strip=True)
            if "advance" in parent_text.lower() and "estimate" in parent_text.lower():
                title_text = parent_text
                break

        if not title_text:
            title_text = filename

        crop_year = parse_crop_year(title_text) or parse_crop_year(filename)
        estimate_num = parse_estimate_number(title_text) or parse_estimate_number(filename)

        if crop_year and estimate_num:
            pdfs.append({
                "url": href,
                "filename": filename,
                "title": title_text[:200],
                "crop_year": crop_year,
                "estimate_num": estimate_num,
            })
            log.info(f"  Found: {estimate_num} AE {crop_year} → {filename}")

    seen = set()
    unique = []
    for p in pdfs:
        key = (p["crop_year"], p["estimate_num"])
        if key not in seen:
            seen.add(key)
            unique.append(p)

    unique.sort(key=lambda x: (x["crop_year"], x["estimate_num"]), reverse=True)
    log.info(f"Discovered {len(unique)} unique estimate PDFs")
    return unique

def filter_unprocessed(pdfs, manifest):
    processed_files = {p["filename"] for p in manifest.get("processed", [])}
    new_pdfs = [p for p in pdfs if p["filename"] not in processed_files]
    if new_pdfs:
        log.info(f"{len(new_pdfs)} new PDF(s) to process")
    else:
        log.info("No new PDFs found — everything up to date")
    return new_pdfs

def select_pdfs_for_processing(new_pdfs, manifest):
    if not new_pdfs:
        return []
    is_bootstrap = len(manifest.get("processed", [])) == 0
    if is_bootstrap:
        selected = [new_pdfs[0]]
        log.info(f"Bootstrap mode: selected {selected[0]['filename']}")
    else:
        selected = sorted(new_pdfs, key=lambda x: (x["crop_year"], x["estimate_num"]))
        log.info(f"Incremental mode: {len(selected)} PDF(s) to process")
    return selected

# ---------------------------------------------------------------------------
# PHASE 2: DOWNLOAD
# ---------------------------------------------------------------------------

def download_pdf(pdf_info):
    url = pdf_info["url"]
    log.info(f"Downloading: {pdf_info['filename']}")
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=120, verify=False)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Download failed: {e}")
        return None
    if resp.content[:5] != b"%PDF-":
        log.error(f"Not a valid PDF: {pdf_info['filename']}")
        return None
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.write(resp.content)
    tmp.close()
    log.info(f"  Saved {len(resp.content)} bytes → {tmp.name}")
    return tmp.name

# ---------------------------------------------------------------------------
# PHASE 3: EXTRACT (robust dual-strategy)
# ---------------------------------------------------------------------------

def find_years_in_text(text):
    """Find all crop year patterns in a string."""
    return re.findall(r"\d{4}-\d{2,4}", text)

def extract_production_from_pdf(pdf_path):
    """
    Extract production data using two strategies:
    Strategy A: pdfplumber table extraction
    Strategy B: text-based line parsing (fallback)
    """
    extracted = {c: {} for c in CANONICAL_COMMODITIES}

    with pdfplumber.open(pdf_path) as pdf:
        log.info(f"  PDF has {len(pdf.pages)} pages")

        # --- Collect all tables and all text from production pages ---
        all_tables = []
        all_text_lines = []
        year_columns = []  # will be populated from header detection

        for page_num, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            text_lower = text.lower()

            # Skip area-only pages
            if "lakh hectare" in text_lower and "lakh tonnes" not in text_lower:
                log.info(f"  Page {page_num}: skipping (area table)")
                continue

            if "lakh tonnes" in text_lower or "lakh metric tonnes" in text_lower:
                log.info(f"  Page {page_num}: production table detected")
            else:
                # Check if it's a continuation (no header but has numbers)
                if not all_tables and not all_text_lines:
                    log.info(f"  Page {page_num}: skipping (no production marker)")
                    continue
                else:
                    log.info(f"  Page {page_num}: continuation page")

            # Collect text lines
            for line in text.split("\n"):
                line = line.strip()
                if line:
                    all_text_lines.append(line)

            # Collect tables
            tables = page.extract_tables()
            if tables:
                for t in tables:
                    if t and len(t) >= 1:
                        all_tables.append(t)
                log.info(f"  Page {page_num}: {len(tables)} table(s), "
                         f"largest has {max(len(t) for t in tables)} rows")
            else:
                log.info(f"  Page {page_num}: no tables detected by pdfplumber")

        # --- STRATEGY A: Table-based extraction ---
        log.info("  Trying Strategy A: table extraction...")
        extracted_a = _extract_from_tables(all_tables)

        # --- STRATEGY B: Text-based extraction ---
        log.info("  Trying Strategy B: text line parsing...")
        extracted_b = _extract_from_text(all_text_lines)

        # Use whichever strategy got more data
        count_a = sum(len(v) for v in extracted_a.values())
        count_b = sum(len(v) for v in extracted_b.values())
        log.info(f"  Strategy A: {count_a} commodity-year pairs")
        log.info(f"  Strategy B: {count_b} commodity-year pairs")

        if count_a >= count_b and count_a > 0:
            extracted = extracted_a
            log.info("  Using Strategy A (tables)")
        elif count_b > 0:
            extracted = extracted_b
            log.info("  Using Strategy B (text)")
        else:
            log.error("  Both strategies failed to extract data")

    # Log summary
    for commodity in CANONICAL_COMMODITIES:
        years = list(extracted[commodity].keys())
        if years:
            log.info(f"  {COMMODITY_DISPLAY[commodity]}: {len(years)} years "
                     f"({min(years)} to {max(years)})")
        else:
            log.warning(f"  {COMMODITY_DISPLAY[commodity]}: NO DATA EXTRACTED")

    return extracted


def _extract_from_tables(all_tables):
    """Strategy A: extract from pdfplumber table objects."""
    extracted = {c: {} for c in CANONICAL_COMMODITIES}
    year_columns = []
    current_crop = None

    for table in all_tables:
        if not table or len(table) < 2:
            continue

        # Try to find header row (any row with multiple year patterns)
        header_idx = -1
        for i, row in enumerate(table[:5]):  # Check first 5 rows for header
            years_found = 0
            for cell in row:
                if cell and re.search(r"\d{4}-\d{2}", str(cell)):
                    years_found += 1
            if years_found >= 3:  # At least 3 year columns
                header_idx = i
                break

        if header_idx >= 0:
            header = table[header_idx]
            year_columns = []
            for ci, cell in enumerate(header):
                if cell is None:
                    continue
                yr = parse_crop_year(str(cell))
                if yr:
                    year_columns.append((ci, yr))
            log.info(f"    Table header at row {header_idx}: "
                     f"{len(year_columns)} year cols "
                     f"({[y for _, y in year_columns[:2]]}...{[y for _, y in year_columns[-1:]]})")
            data_rows = table[header_idx + 1:]
        else:
            data_rows = table

        if not year_columns:
            # Try to detect years from ANY cell in the table
            for row in table[:3]:
                for ci, cell in enumerate(row):
                    if cell and re.search(r"\d{4}-\d{2}", str(cell)):
                        yr = parse_crop_year(str(cell))
                        if yr and (ci, yr) not in year_columns:
                            year_columns.append((ci, yr))
            if year_columns:
                log.info(f"    Inferred {len(year_columns)} year columns from scattered cells")
                data_rows = table
            else:
                continue

        # Process data rows
        for row in data_rows:
            if not row:
                continue

            # Find crop name and season in the row
            crop_found = None
            season_found = None
            has_unknown_crop = False

            for ci, cell in enumerate(row):
                val = str(cell or "").strip()
                if not val:
                    continue

                # Check if this cell is a crop name
                canonical = normalize_crop_name(val)
                if canonical and not is_aggregate_row(val):
                    crop_found = canonical
                elif not canonical and not is_aggregate_row(val):
                    # Check if this looks like a crop name we don't track
                    # (alphabetic, not a season, not a number, not a symbol)
                    clean = re.sub(r"^\d+[\.\)]*\s*", "", val).strip()
                    if (len(clean) > 2
                            and not re.match(r"^[\d\.\,\-\@\$\#\*\s]+$", clean)
                            and not normalize_season(clean)
                            and not re.search(r"\d{4}-\d{2}", clean)
                            and clean.lower() not in ("s no", "s no.", "s.no", "s.no.",
                                "crop", "season", "production", "sl", "sl.")):
                        has_unknown_crop = True

                # Check if this cell is a season
                s = normalize_season(val)
                if s:
                    season_found = s

            if crop_found:
                current_crop = crop_found
            elif has_unknown_crop:
                current_crop = None  # Reset: new crop we don't track
            if is_aggregate_row(" ".join(str(c or "") for c in row)):
                current_crop = None
                continue
            if current_crop is None or season_found is None:
                continue

            # Extract values for year columns
            for col_idx, year_str in year_columns:
                if col_idx < len(row):
                    value = parse_number(row[col_idx])
                    if value is not None:
                        if year_str not in extracted[current_crop]:
                            extracted[current_crop][year_str] = {
                                "kharif": None, "rabi": None,
                                "summer": None, "total": None,
                            }
                        extracted[current_crop][year_str][season_found] = value

    return extracted


def _extract_from_text(text_lines):
    """Strategy B: extract from raw text lines using regex parsing."""
    extracted = {c: {} for c in CANONICAL_COMMODITIES}

    # First pass: find the year header line
    year_columns = []  # list of year strings in order
    for line in text_lines:
        years = re.findall(r"(\d{4}-\d{2,4})", line)
        if len(years) >= 3:
            # Normalize years
            year_columns = []
            for y in years:
                parts = y.split("-")
                end = parts[1]
                if len(end) == 4:
                    end = end[2:]
                year_columns.append(f"{parts[0]}-{end}")
            log.info(f"    Year header found: {len(year_columns)} years "
                     f"({year_columns[0]}...{year_columns[-1]})")
            break

    if not year_columns:
        log.warning("    No year header found in text")
        return extracted

    # Second pass: find crop + season lines with numeric data
    current_crop = None

    for line in text_lines:
        # Skip header/title lines
        if "ministry" in line.lower() or "department" in line.lower():
            continue
        if "lakh tonnes" in line.lower() or "lakh hectare" in line.lower():
            continue

        # Extract all numbers from the line
        # Split line into tokens
        tokens = re.split(r"\s{2,}|\t+", line)  # Split on 2+ spaces or tabs
        if not tokens:
            continue

        # Check for crop name in the line
        line_lower = line.lower()
        crop_in_line = None
        has_unknown_crop = False

        for alias, canonical in CROP_ALIASES.items():
            # Word boundary match
            if re.search(r"\b" + re.escape(alias) + r"\b", line_lower):
                if not is_aggregate_row(line):
                    crop_in_line = canonical
                    break

        # Check if line has a non-target crop name (reset carry-forward)
        if not crop_in_line and not is_aggregate_row(line):
            non_target_crops = [
                "urad", "moong", "lentil", "masoor", "pea", "kulthi",
                "moth", "khesari", "rajma", "cowpea",
                "groundnut", "soyabean", "soybean", "sunflower", "sesamum",
                "rapeseed", "mustard", "linseed", "castor", "niger",
                "safflower", "cotton", "jute", "mesta", "tobacco",
                "tea", "coffee", "rubber", "coconut", "arecanut",
                "pepper", "cardamom", "turmeric", "ginger", "chilli",
                "coriander", "cumin", "garlic", "tapioca", "potato",
                "sweet potato", "onion",
            ]
            for nc in non_target_crops:
                if re.search(r"\b" + re.escape(nc) + r"\b", line_lower):
                    has_unknown_crop = True
                    break

        if crop_in_line:
            current_crop = crop_in_line
        elif has_unknown_crop:
            current_crop = None

        # Check for season
        season = None
        if re.search(r"\bkharif\b", line_lower):
            season = "kharif"
        elif re.search(r"\brabi\b", line_lower):
            season = "rabi"
        elif re.search(r"\bsummer\b", line_lower):
            season = "summer"
        elif re.search(r"\btotal\b", line_lower):
            season = "total"

        if current_crop is None or season is None:
            continue

        if is_aggregate_row(line):
            current_crop = None
            continue

        # Extract numeric values from the line
        numbers = []
        for token in re.findall(r"[\d]+\.?\d*", line):
            try:
                n = float(token)
                # Filter out serial numbers (typically 1-30) and very small values
                # Production values in LMT are typically > 1 for our commodities
                if n > 0.5:
                    numbers.append(n)
            except ValueError:
                pass

        if not numbers:
            continue

        # Map numbers to years
        # The numbers should align with the year columns
        # Sometimes the first number(s) might be serial numbers — skip small ints
        # Filter to only plausible production values
        values = []
        for n in numbers:
            # Serial numbers are typically integers < 30
            if n == int(n) and n < 30 and len(values) == 0:
                continue  # Skip likely serial number at start
            values.append(n)

        # Align values to year columns (take last N values matching year count)
        if len(values) >= len(year_columns):
            values = values[:len(year_columns)]
        elif len(values) < len(year_columns):
            # Pad from left with None (earlier years might be missing)
            pad = len(year_columns) - len(values)
            values = [None] * pad + values

        for yi, year_str in enumerate(year_columns):
            val = values[yi] if yi < len(values) else None
            if val is not None:
                if year_str not in extracted[current_crop]:
                    extracted[current_crop][year_str] = {
                        "kharif": None, "rabi": None,
                        "summer": None, "total": None,
                    }
                extracted[current_crop][year_str][season] = val

        log.info(f"    {COMMODITY_DISPLAY.get(current_crop, current_crop)} "
                 f"{season}: {len([v for v in values if v])} values")

    return extracted


# ---------------------------------------------------------------------------
# PHASE 4: MERGE
# ---------------------------------------------------------------------------

def merge_commodity(existing_data, new_data):
    """
    Merge new year/season data into an existing commodity's data array.
    Returns (updated_data_list, change_count).
    """
    existing_by_year = {}
    for entry in existing_data:
        existing_by_year[entry["year"]] = entry

    changes = 0
    for year_str, seasons in new_data.items():
        if year_str not in existing_by_year:
            entry = {"year": year_str}
            for s in VALID_SEASONS:
                entry[s] = seasons.get(s)
            existing_by_year[year_str] = entry
            changes += 1
        else:
            entry = existing_by_year[year_str]
            for s in VALID_SEASONS:
                new_val = seasons.get(s)
                if new_val is not None:
                    if entry.get(s) != new_val:
                        changes += 1
                    entry[s] = new_val

    all_entries = sorted(
        existing_by_year.values(),
        key=lambda x: x["year"],
        reverse=True,
    )
    return all_entries, changes


# ---------------------------------------------------------------------------
# PHASE 5: WRITE
# ---------------------------------------------------------------------------

def process_pdf(pdf_info, manifest):
    log.info(f"Processing: {parse_estimate_label(pdf_info['estimate_num'])} "
             f"{pdf_info['crop_year']}")

    pdf_path = download_pdf(pdf_info)
    if not pdf_path:
        return False

    try:
        extracted = extract_production_from_pdf(pdf_path)

        total_values = sum(len(years) for years in extracted.values())
        if total_values == 0:
            log.error("No production data extracted from PDF — skipping")
            return False

        # Load single production.json
        pj = load_production_json()

        total_changes = 0
        for commodity in CANONICAL_COMMODITIES:
            if not extracted[commodity]:
                log.warning(f"  No data for {COMMODITY_DISPLAY[commodity]} — skipping")
                continue

            # Ensure commodity exists in structure
            if commodity not in pj["commodities"]:
                pj["commodities"][commodity] = {
                    "name": COMMODITY_DISPLAY[commodity],
                    "data": []
                }

            existing_data = pj["commodities"][commodity]["data"]
            updated_data, changes = merge_commodity(existing_data, extracted[commodity])
            pj["commodities"][commodity]["data"] = updated_data
            total_changes += changes
            log.info(f"  {COMMODITY_DISPLAY[commodity]}: "
                     f"{len(updated_data)} years, {changes} change(s)")

        # Update meta
        now = datetime.now(timezone.utc).isoformat()
        pj["meta"]["last_updated"] = now
        pj["meta"]["source_pdf"] = pdf_info["filename"]
        pj["meta"]["estimate_type"] = parse_estimate_label(pdf_info["estimate_num"])
        pj["meta"]["estimate_year"] = pdf_info["crop_year"]

        # Save single file
        save_production_json(pj)

        manifest["processed"].append({
            "filename": pdf_info["filename"],
            "estimate": f"{pdf_info['estimate_num']}",
            "crop_year": pdf_info["crop_year"],
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "url": pdf_info["url"],
        })
        manifest["last_run"] = datetime.now(timezone.utc).isoformat()
        save_manifest(manifest)

        log.info(f"Done: {total_changes} total change(s) written")
        return True

    finally:
        try:
            os.unlink(pdf_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    log.info("=" * 60)
    log.info("DA&FW Production Extractor — Starting")
    log.info("=" * 60)

    ensure_data_dir()
    manifest = load_manifest()

    try:
        all_pdfs = discover_pdfs()
    except requests.RequestException as e:
        log.error(f"Failed to fetch index page: {e}")
        sys.exit(1)

    if not all_pdfs:
        log.warning("No PDFs found on the page — exiting")
        sys.exit(0)

    new_pdfs = filter_unprocessed(all_pdfs, manifest)
    to_process = select_pdfs_for_processing(new_pdfs, manifest)

    if not to_process:
        log.info("Nothing to process — exiting")
        sys.exit(0)

    success_count = 0
    for pdf_info in to_process:
        ok = process_pdf(pdf_info, manifest)
        if ok:
            success_count += 1

    log.info("=" * 60)
    log.info(f"Finished: {success_count}/{len(to_process)} PDF(s) processed successfully")
    log.info("=" * 60)

    if success_count == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

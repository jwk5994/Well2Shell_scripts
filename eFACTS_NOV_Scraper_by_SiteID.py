import asyncio
import re
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright


# ============================================================
# CONFIGURATION
# ============================================================
#Add predefined Site ID list here

SITE_IDS = [
]

BASE_URL = "https://www.ahs.dep.pa.gov/eFACTSWeb/"
SITE_URL = BASE_URL + "searchResults_singleSite.aspx?SiteID={}"
VIOLATION_URL = BASE_URL + "searchResults_singleViol.aspx?InspectionID={}"

# Number of Site IDs processed simultaneously.
MAX_CONCURRENT = 5

OUTPUT_FILE = Path.cwd() / "Output_Name_Here.csv"
ERROR_FILE = Path.cwd() / "Error_File_Name_Here.csv"
PROCESSED_FILE = Path.cwd() / "Processed_File_Name_Here.csv"


# ============================================================
# BASIC HELPERS
# ============================================================

def clean(text):
    if text is None:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def extract_query_id(href, parameter):
    if not href:
        return None

    match = re.search(
        rf"{re.escape(parameter)}=(\d+)",
        href,
        flags=re.IGNORECASE
    )

    return match.group(1) if match else None


    return ""


# ============================================================
# VIOLATION FIELD EXTRACTION
# ============================================================

def extract_pa_code(row):
    """
    Extract the PA Code Legal Citation from a violation row.

    Example:
    PA Code Legal Citation: 25 Pa. Code 245.533 : PA Code Website
    """

    text = clean(row.get_text(" ", strip=True))

    match = re.search(
        r"Legal\s+Citation\s*:\s*(.*?)(?:\s+PA\s+Code\s+Website|$)",
        text,
        flags=re.IGNORECASE
    )

    if not match:
        return ""

    citation = clean(match.group(1))

    # Remove trailing colon/whitespace.
    citation = re.sub(r"\s*:\s*$", "", citation)

    return citation


def extract_violation_type(row):
    """
    Extract:

    Violation Type: Environmental Health & Safety
    """

    text = clean(row.get_text(" ", strip=True))

    match = re.search(
        r"Violation\s+Type\s*:\s*(.*)$",
        text,
        flags=re.IGNORECASE
    )

    if not match:
        return ""

    return clean(match.group(1))


# ============================================================
# VIOLATION TABLE PARSER
# ============================================================

def parse_violation_table(
    table,
    site_id,
    inspection_id
):
    """
    Parse one eFACTS violation table.

    Related Enforcements are deliberately ignored.
    """

    records = []

    current = None

    rows = table.find_all("tr")

    for row in rows:

        cells = row.find_all("td")

        if not cells:
            continue

        cell_text = [
            clean(cell.get_text(" ", strip=True))
            for cell in cells
        ]

        # ----------------------------------------------------
        # Detect beginning of a new violation.
        #
        # Typical structure:
        #
        # <td rowspan="6">8288507</td>
        # <td rowspan="6">09/02/2026</td>
        # <td>Failure to...</td>
        # ----------------------------------------------------

        if len(cells) >= 3:

            violation_id = cell_text[0]
            violation_date = cell_text[1]
            description = cell_text[2]

            if (
                re.fullmatch(r"\d+", violation_id)
                and re.fullmatch(r"\d{2}/\d{2}/\d{4}", violation_date)
            ):

                # Save previous violation.
                if current:
                    records.append(current)

                current = {
                    "SITE_ID": site_id,
                    "INSPECTION_ID": inspection_id,
                    "VIOLATION_ID": violation_id,
                    "VIOLATION_DATE": violation_date,
                    "VIOLATION_DESCRIPTION": description,
                    "PA_CODE_LEGAL_CITATION": "",
                    "VIOLATION_TYPE": "",
                }

                continue

        # ----------------------------------------------------
        # Additional rows belonging to current violation.
        # ----------------------------------------------------

        if current:

            row_text = clean(row.get_text(" ", strip=True))

            # Ignore Related Enforcements entirely.
            if "Related Enforcements" in row_text:
                continue

            pa_code = extract_pa_code(row)

            if pa_code:
                current["PA_CODE_LEGAL_CITATION"] = pa_code

            violation_type = extract_violation_type(row)

            if violation_type:
                current["VIOLATION_TYPE"] = violation_type

    # Save final violation.
    if current:
        records.append(current)

    return records


# ============================================================
# VIOLATION PAGE PARSER
# ============================================================

def parse_violation_page(
    html,
    site_id,
    inspection_id
):

    soup = BeautifulSoup(html, "html.parser")


    records = []

    # There may be one or more violation tables.
    tables = soup.select("table.GridViewTable")

    for table in tables:

        table_records = parse_violation_table(
            table=table,
            site_id=site_id,
            inspection_id=inspection_id,
        )

        records.extend(table_records)

    return records


# ============================================================
# SITE PAGE PARSER
# ============================================================

def parse_site_page(html, site_id):

    soup = BeautifulSoup(html, "html.parser")

    inspection_ids = []

    # Find the table containing:
    # Site-Level and Primary Facility-Level Inspections
    inspection_table = None

    for table in soup.find_all("table"):

        text = clean(table.get_text(" ", strip=True))

        if "Site-Level and Primary Facility-Level Inspections" in text:
            inspection_table = table
            break

    # Fallback: find the heading and use the next table.
    if inspection_table is None:

        heading = soup.find(
            string=re.compile(
                r"Site-Level and Primary Facility-Level Inspections",
                re.IGNORECASE
            )
        )

        if heading:

            parent = heading.parent

            inspection_table = parent.find_next("table")

    if inspection_table is None:
        return []

    # --------------------------------------------------------
    # Find inspection rows with "Violation(s) Noted"
    # --------------------------------------------------------

    for row in inspection_table.find_all("tr"):

        row_text = clean(row.get_text(" ", strip=True))

        if "Violation(s) Noted" not in row_text:
            continue

        for link in row.find_all("a"):

            href = link.get("href", "")

            inspection_id = extract_query_id(
                href,
                "InspectionID"
            )

            if inspection_id:
                inspection_ids.append(inspection_id)

    # Remove duplicates while preserving order.
    return list(dict.fromkeys(inspection_ids))


# ============================================================
# PROCESS ONE SITE
# ============================================================

async def process_site(
    request,
    site_id,
    semaphore
):

    async with semaphore:

        site_id = str(site_id)

        records = []
        errors = []

        try:

            print(f"[{site_id}] Loading site page...")

            response = await request.get(
                SITE_URL.format(site_id),
                timeout=200000
            )

            if not response.ok:

                errors.append({
                    "SITE_ID": site_id,
                    "INSPECTION_ID": "",
                    "ERROR": (
                        f"Site page returned HTTP "
                        f"{response.status}"
                    )
                })

                return site_id, records, errors

            html = await response.text()

            inspection_ids = parse_site_page(
                html,
                site_id
            )

            print(
                f"[{site_id}] "
                f"Found {len(inspection_ids)} NOV inspection(s)."
            )

            # ------------------------------------------------
            # Download violation pages.
            # ------------------------------------------------

            for inspection_id in inspection_ids:

                try:

                    print(
                        f"[{site_id}] "
                        f"  Fetching inspection {inspection_id}..."
                    )

                    violation_response = await request.get(
                        VIOLATION_URL.format(inspection_id),
                        timeout=200000
                    )

                    if not violation_response.ok:

                        errors.append({
                            "SITE_ID": site_id,
                            "INSPECTION_ID": inspection_id,
                            "ERROR": (
                                f"Violation page returned HTTP "
                                f"{violation_response.status}"
                            )
                        })

                        continue

                    violation_html = (
                        await violation_response.text()
                    )

                    violation_records = parse_violation_page(
                        violation_html,
                        site_id,
                        inspection_id
                    )

                    records.extend(violation_records)

                except Exception as e:

                    errors.append({
                        "SITE_ID": site_id,
                        "INSPECTION_ID": inspection_id,
                        "ERROR": str(e)
                    })

            print(
                f"[{site_id}] COMPLETE — "
                f"{len(records)} violation record(s)."
            )

            return site_id, records, errors

        except Exception as e:

            errors.append({
                "SITE_ID": site_id,
                "INSPECTION_ID": "",
                "ERROR": str(e)
            })

            print(
                f"[{site_id}] ERROR: {e}"
            )

            return site_id, records, errors


# ============================================================
# CHECKPOINT FUNCTIONS
# ============================================================

def load_checkpoint():

    records = []
    errors = []
    processed = set()

    # --------------------------------------------------------
    # Previous data
    # --------------------------------------------------------

    if OUTPUT_FILE.exists():

        try:

            df = pd.read_csv(
                OUTPUT_FILE,
                dtype=str
            )

            if not df.empty:
                records = df.to_dict("records")

            print(
                f"Loaded {len(records)} previous "
                f"violation records."
            )

        except Exception as e:

            print(
                f"Warning: could not load output file: {e}"
            )

    # --------------------------------------------------------
    # Previous errors
    # --------------------------------------------------------

    if ERROR_FILE.exists():

        try:

            df = pd.read_csv(
                ERROR_FILE,
                dtype=str
            )

            if not df.empty:
                errors = df.to_dict("records")

        except Exception:
            pass

    # --------------------------------------------------------
    # Processed sites
    # --------------------------------------------------------

    if PROCESSED_FILE.exists():

        try:

            df = pd.read_csv(
                PROCESSED_FILE,
                dtype=str
            )

            if "SITE_ID" in df.columns:

                processed = set(
                    df["SITE_ID"]
                    .dropna()
                    .astype(str)
                )

            print(
                f"Loaded {len(processed)} "
                f"previously processed sites."
            )

        except Exception as e:

            print(
                f"Warning: could not load "
                f"processed-sites file: {e}"
            )

    return records, errors, processed


def save_checkpoint(
    records,
    errors,
    processed
):

    # --------------------------------------------------------
    # Save violation data
    # --------------------------------------------------------

    columns = [
        "SITE_ID",
        "INSPECTION_ID",
        "VIOLATION_ID",
        "VIOLATION_DATE",
        "VIOLATION_DESCRIPTION",
        "PA_CODE_LEGAL_CITATION",
        "VIOLATION_TYPE",
    ]

    df = pd.DataFrame(
        records,
        columns=columns
    )

    df.to_csv(
        OUTPUT_FILE,
        index=False
    )

    # --------------------------------------------------------
    # Save errors
    # --------------------------------------------------------

    if errors:

        error_df = pd.DataFrame(errors)

        error_df.to_csv(
            ERROR_FILE,
            index=False
        )

    # --------------------------------------------------------
    # Save processed Site IDs
    # --------------------------------------------------------

    processed_df = pd.DataFrame(
        sorted(processed),
        columns=["SITE_ID"]
    )

    processed_df.to_csv(
        PROCESSED_FILE,
        index=False
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    print()
    print("=" * 70)
    print("PA DEP eFACTS NOV SCRAPER")
    print("=" * 70)
    print()

    records, errors, processed = load_checkpoint()

    # Convert all IDs to strings.
    site_ids = [
        str(site_id)
        for site_id in SITE_IDS
    ]

    # Only process sites not already completed.
    remaining = [
        site_id
        for site_id in site_ids
        if site_id not in processed
    ]

    print()
    print(
        f"Total Site IDs:       {len(site_ids)}"
    )
    print(
        f"Already processed:    {len(processed)}"
    )
    print(
        f"Remaining:             {len(remaining)}"
    )
    print()

    if not remaining:

        print("Nothing new to process.")
        print()
        print(f"Output: {OUTPUT_FILE}")
        return

    semaphore = asyncio.Semaphore(
        MAX_CONCURRENT
    )

    async with async_playwright() as p:

        # ----------------------------------------------------
        # HTTP client rather than browser.
        # ----------------------------------------------------

        request = await p.request.new_context(

            user_agent=(
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),

            extra_http_headers={
                "Accept": (
                    "text/html,application/xhtml+xml,"
                    "application/xml;q=0.9,*/*;q=0.8"
                ),
                "Accept-Language": (
                    "en-US,en;q=0.9"
                ),
            },

            ignore_https_errors=True,
        )

        try:

            # ------------------------------------------------
            # Launch all remaining sites concurrently.
            # ------------------------------------------------

            tasks = [
                asyncio.create_task(
                    process_site(
                        request,
                        site_id,
                        semaphore
                    )
                )
                for site_id in remaining
            ]

            # ------------------------------------------------
            # Process results as individual sites finish.
            # This allows checkpointing after EVERY site.
            # ------------------------------------------------

            for task in asyncio.as_completed(tasks):

                site_id, new_records, new_errors = (
                    await task
                )

                # Add new records.
                records.extend(new_records)

                # Add errors.
                errors.extend(new_errors)

                # Mark site processed.
                processed.add(site_id)

                # ------------------------------------------------
                # Checkpoint immediately.
                # ------------------------------------------------

                save_checkpoint(
                    records,
                    errors,
                    processed
                )

                print()
                print(
                    f"CHECKPOINT SAVED — "
                    f"{site_id}"
                )
                print(
                    f"Total violation records: "
                    f"{len(records)}"
                )
                print(
                    f"Sites processed: "
                    f"{len(processed)} / {len(site_ids)}"
                )
                print()

        finally:

            await request.dispose()

    print()
    print("=" * 70)
    print("SCRAPING COMPLETE")
    print("=" * 70)
    print()
    print(
        f"Violation records: {len(records)}"
    )
    print(
        f"Processed sites:   {len(processed)}"
    )
    print(
        f"Errors:            {len(errors)}"
    )
    print()
    print(f"Output file:       {OUTPUT_FILE}")
    print(f"Error file:        {ERROR_FILE}")
    print(f"Processed file:    {PROCESSED_FILE}")
    print()


# ============================================================
# WINDOWS ENTRY POINT
# ============================================================

if __name__ == "__main__":

    # Required for Windows multiprocessing/event-loop behavior.
    asyncio.run(main())

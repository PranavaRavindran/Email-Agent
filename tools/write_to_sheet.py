import json
import os
import re
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build

from auth import get_gmail_service
from tools.get_email_detail import get_fetched_ids
from tools.search_email_ids import get_last_search_count, get_last_search_range

_SPREADSHEET_ID = "1NlX-ND9UIQkalliOqsw4eb9ItcMNllo-dkuEwWzvI3g"
_READ_RANGE = "Tracker!A3:F"
_FIRST_DATA_ROW = 3

_PENDING_WRITE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pending_write.json"
)

_STALE_PENDING_WRITE_AGE = timedelta(hours=1)

_LEGAL_SUFFIXES = {"llc", "inc", "corp", "corporation", "ltd", "co", "plc"}
_PARENS_RE = re.compile(r"\([^)]*\)")
_TRAILING_SEGMENT_RE = re.compile(r"[,\-–].*$")
_PUNCTUATION_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")

_VALID_STATUSES = ("Applied", "Rejected", "Interviewing", "Offer")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LEADING_DIGITS_RE = re.compile(r"^\d+\s+")


def _normalize(text: str) -> str:
    """Returns a comparison key for matching companies/roles across runs,
    tolerant of legal-suffix, location-suffix, and reference-number drift."""
    text = text.lower()
    text = _PARENS_RE.sub("", text)

    words = text.split()
    while words and re.sub(r"[^\w]", "", words[-1]) in _LEGAL_SUFFIXES:
        words.pop()
    text = " ".join(words)

    text = _TRAILING_SEGMENT_RE.sub("", text)
    text = _PUNCTUATION_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()

    return text


def _validate_entry(entry: dict, range_start: str = "", range_end: str = ""):
    """Validates a single incoming entry.

    Returns a (cleaned_entry, reason) tuple. reason is None if the entry is
    valid, in which case cleaned_entry has its status corrected for
    whitespace/case drift and its source defaulted if missing. If reason is
    not None, cleaned_entry is unused - the caller should keep the original
    entry alongside the reason.

    If both range_start and range_end are provided, the entry's date must
    fall within that inclusive range. Both bounds are inclusive, since Gmail
    returns boundary-date messages for after:/before: queries.
    """
    company = entry.get("company")
    role = entry.get("role")
    date = entry.get("date")
    status = entry.get("status")

    if not company or not str(company).strip():
        return entry, "missing or empty company"
    if not role or not str(role).strip():
        return entry, "missing or empty role"
    if not date or not str(date).strip():
        return entry, "missing or empty date"
    if not status or not str(status).strip():
        return entry, "missing or empty status"

    company = str(company)
    role = str(role)

    if _LEADING_DIGITS_RE.match(company.strip()):
        return entry, "company starts with a leading run of digits (HTML-extraction artifact)"
    if _LEADING_DIGITS_RE.match(role.strip()):
        return entry, "role starts with a leading run of digits (HTML-extraction artifact)"
    if "[truncated]" in company:
        return entry, "company contains '[truncated]'"
    if "[truncated]" in role:
        return entry, "role contains '[truncated]'"

    date_str = str(date).strip()
    if not _DATE_RE.match(date_str):
        return entry, f"date '{date_str}' does not match YYYY-MM-DD"
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return entry, f"date '{date_str}' is not a real date"

    if range_start and range_end and not (range_start <= date_str <= range_end):
        return entry, f"date {date_str} is outside the searched range {range_start} to {range_end}"

    status_stripped = str(status).strip()
    corrected_status = None
    for valid_status in _VALID_STATUSES:
        if status_stripped.lower() == valid_status.lower():
            corrected_status = valid_status
            break
    if corrected_status is None:
        return entry, f"status '{status}' is not one of {', '.join(_VALID_STATUSES)}"

    cleaned = dict(entry)
    cleaned["company"] = company
    cleaned["role"] = role
    cleaned["date"] = date_str
    cleaned["status"] = corrected_status
    if not cleaned.get("source") or not str(cleaned.get("source")).strip():
        cleaned["source"] = "Email"

    return cleaned, None


def _company_matches(company_a: str, company_b: str) -> bool:
    """Two normalized company keys match if they're equal, or if one is a
    whitespace-boundary prefix of the other (handles truncated extractions
    like "crosslink" vs "crosslink professional tax solutions")."""
    if company_a == company_b:
        return True
    shorter, longer = sorted((company_a, company_b), key=len)
    if not shorter or len(shorter) >= len(longer):
        return False
    return longer.startswith(shorter) and longer[len(shorter)] == " "


def _keys_match(company_a: str, role_a: str, company_b: str, role_b: str) -> bool:
    return role_a == role_b and _company_matches(company_a, company_b)


def _sort_key(entry):
    try:
        return (0, datetime.fromisoformat(entry["date"].strip()))
    except (KeyError, ValueError, AttributeError):
        return (1, datetime.max)


def _merge_duplicates(valid_entries: list) -> tuple:
    """Groups entries by normalized company+role and merges each group into a
    single entry: earliest date, latest status, longest company and role
    strings. Returns (deduped_entries, duplicates_in_batch)."""
    groups = {}
    group_order = []
    for entry in valid_entries:
        key = (_normalize(entry["company"]), _normalize(entry["role"]))
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(entry)

    duplicates_in_batch = []
    deduped_entries = []
    for key in group_order:
        group = groups[key]
        if len(group) == 1:
            deduped_entries.append(group[0])
            continue

        earliest_date = min(group, key=lambda e: e["date"])["date"]
        latest_status = max(group, key=lambda e: e["date"])["status"]
        longest_company = max(group, key=lambda e: len(e["company"]))["company"]
        longest_role = max(group, key=lambda e: len(e["role"]))["role"]

        merged = dict(group[0])
        merged["date"] = earliest_date
        merged["status"] = latest_status
        merged["company"] = longest_company
        merged["role"] = longest_role
        deduped_entries.append(merged)

        duplicates_in_batch.append({
            "company": longest_company,
            "role": longest_role,
            "date": earliest_date,
            "status": latest_status,
        })
        print(
            f"[stage_write] merged duplicate: {longest_company} / {longest_role} -> "
            f"date {earliest_date}, status {latest_status}"
        )

    return deduped_entries, duplicates_in_batch


def _resolve(entries: list) -> dict:
    """Sorts and normalizes entries, reads the existing Tracker rows, and runs
    the matching logic against them. Does NOT write anything to the sheet -
    callers decide whether to stage the result or apply it.

    Row 1 of the sheet holds a table title and row 2 holds the headers (Date,
    Company, Role, Link, Source, Status), so data starts at row 3. Matching
    uses normalized company and role only; date is deliberately excluded
    because it drifts between runs. Column A is left blank for a new row if
    its date matches the row above it, and column D (Link) is always left
    blank.
    """
    entries = sorted(entries, key=_sort_key)

    print(f"[_resolve] resolving {len(entries)} entries")

    gmail_service = get_gmail_service()
    sheets_service = build("sheets", "v4", credentials=gmail_service._http.credentials)

    existing_rows = sheets_service.spreadsheets().values().get(
        spreadsheetId=_SPREADSHEET_ID,
        range=_READ_RANGE,
    ).execute().get("values", [])

    print(f"Read {len(existing_rows)} existing rows from Tracker sheet")

    # existing_rows[idx] is sheet row _FIRST_DATA_ROW + idx, since data starts at row 3.
    # Matching uses normalized (company, role) only - date is excluded because it
    # drifts across runs and dates for the same application would otherwise collide.
    # Company matching also tolerates one normalized key being a whitespace-boundary
    # prefix of the other, to handle truncated company names; role must match exactly.
    row_records = []
    last_effective_date = None
    last_data_row = _FIRST_DATA_ROW - 1
    for idx, row in enumerate(existing_rows):
        row_number = _FIRST_DATA_ROW + idx
        date_cell = row[0].strip() if len(row) > 0 else ""
        if date_cell:
            last_effective_date = date_cell
        if len(row) > 1 and row[1].strip():
            last_data_row = row_number
        if len(row) > 2:
            company = row[1].strip()
            role = row[2].strip()
            status = row[5].strip() if len(row) > 5 else ""
            row_records.append({
                "row_number": row_number,
                "status": status,
                "company": company,
                "role": role,
                "company_norm": _normalize(company),
                "role_norm": _normalize(role),
            })

    # Flag existing sheet rows that share an exact normalized key - a data
    # quality issue in the sheet itself. The first (lowest-numbered) row among
    # them is the one matching will use, since row_records preserves row order
    # and matching below takes the first match found.
    rows_by_exact_key = {}
    for record in row_records:
        rows_by_exact_key.setdefault((record["company_norm"], record["role_norm"]), []).append(record)
    for records in rows_by_exact_key.values():
        if len(records) > 1:
            row_numbers = [r["row_number"] for r in records]
            print(
                f"[stage_write] WARNING existing sheet rows {row_numbers} share a normalized key "
                f"({records[0]['company']} / {records[0]['role']})"
            )

    updates = []
    rows_to_append = []
    new_rows = []
    status_changes = []
    unchanged_count = 0
    matched_row_numbers = set()

    for entry in entries:
        entry_date = entry["date"].strip()
        entry_company_norm = _normalize(entry["company"])
        entry_role_norm = _normalize(entry["role"])
        match = None
        for record in row_records:
            if _keys_match(entry_company_norm, entry_role_norm, record["company_norm"], record["role_norm"]):
                match = record
                break

        if match is not None:
            row_number, existing_status = match["row_number"], match["status"]
            matched_row_numbers.add(row_number)
            print(f"Matched entry ({entry['company']} / {entry['role']}) to existing row {row_number}")
            new_status = entry["status"]
            if new_status == existing_status:
                print(f"Status unchanged for existing row {row_number} ({entry['company']} / {entry['role']})")
                unchanged_count += 1
            else:
                print(f"Updating status of row {row_number} ({entry['company']} / {entry['role']}) to '{new_status}'")
                updates.append({
                    "range": f"Tracker!F{row_number}",
                    "values": [[new_status]],
                })
                status_changes.append({
                    "company": entry["company"],
                    "role": entry["role"],
                    "old_status": existing_status,
                    "new_status": new_status,
                })
        else:
            print(f"No existing row matched ({entry['company']} / {entry['role']}); treating as new")
            print(f"Adding new row for {entry['company']} / {entry['role']} ({entry_date})")
            date_column = "" if entry_date == last_effective_date else entry_date
            last_effective_date = entry_date
            rows_to_append.append([
                date_column, entry["company"], entry["role"], "", entry["source"], entry["status"],
            ])
            new_rows.append(entry)

    unseen_rows = [
        {"company": record["company"], "role": record["role"]}
        for record in row_records
        if record["row_number"] not in matched_row_numbers
    ]

    return {
        "sheets_service": sheets_service,
        "entries": entries,
        "updates": updates,
        "rows_to_append": rows_to_append,
        "last_data_row": last_data_row,
        "new_rows": new_rows,
        "status_changes": status_changes,
        "unchanged_count": unchanged_count,
        "unseen_rows": unseen_rows,
    }


def _apply(sheets_service, updates: list, rows_to_append: list, last_data_row: int) -> dict:
    """Writes a resolved set of updates and new rows to the Tracker sheet."""
    if updates:
        sheets_service.spreadsheets().values().batchUpdate(
            spreadsheetId=_SPREADSHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": updates},
        ).execute()
        print(f"Updated {len(updates)} rows in Tracker sheet")

    if rows_to_append:
        start_row = last_data_row + 1
        end_row = start_row + len(rows_to_append) - 1
        sheets_service.spreadsheets().values().update(
            spreadsheetId=_SPREADSHEET_ID,
            range=f"Tracker!A{start_row}:F{end_row}",
            valueInputOption="USER_ENTERED",
            body={"values": rows_to_append},
        ).execute()
        print(f"Wrote {len(rows_to_append)} rows to Tracker sheet starting at row {start_row}")

    rows_added = len(rows_to_append)
    rows_updated = len(updates)
    print(f"Done: {rows_added} added, {rows_updated} updated")

    return {"rows_added": rows_added, "rows_updated": rows_updated}


def stage_write(entries: list) -> dict:
    """Validates and resolves job application entries against the Tracker
    sheet and stages the result for confirmation, without writing anything to
    the sheet yet.

    The searched date range is not taken from the caller - it is derived from
    the most recent search_email_ids query via get_last_search_range(). Both
    bounds are treated as inclusive, since Gmail returns boundary-date
    messages for after:/before: queries. If either bound is empty (the query
    had no date bounds), the date-range check is skipped entirely.

    Args:
        entries: List of dicts, each with keys: date, company, role, source, status.

    Returns:
        A dict with new_rows_count, status_changes_count, unchanged_count, the
        details of new_rows and status_changes (but not the unchanged entries
        themselves), duplicates_in_batch, unseen_rows, invalid_entries,
        fetch_gap (if fewer emails were fetched than were searched), and a
        has_changes flag. If entries is empty, returns an error dict instead
        and does not write pending_write.json. If a search was run but no
        emails were fetched at all, returns an error dict instead and stages
        nothing, since entries could not have been derived from email
        content.
    """
    if not entries:
        return {"error": "No entries were provided; stage_write requires at least one entry."}

    searched = get_last_search_count()
    fetched = len(get_fetched_ids())
    if searched > 0 and fetched == 0:
        print(f"[stage_write] REFUSED — {searched} ids searched, 0 emails read")
        return {
            "error": (
                f"No emails were read. search_email_ids returned {searched} ids but "
                "get_email_detail was never called, so any entries provided were not "
                "derived from email content. Fetch every id before staging."
            )
        }

    range_start, range_end = get_last_search_range()

    invalid_entries = []
    valid_entries = []
    for entry in entries:
        cleaned, reason = _validate_entry(entry, range_start, range_end)
        if reason is not None:
            invalid_entries.append({"entry": entry, "reason": reason})
        else:
            valid_entries.append(cleaned)

    deduped_entries, duplicates_in_batch = _merge_duplicates(valid_entries)

    resolved = _resolve(deduped_entries)

    new_rows = resolved["new_rows"]
    status_changes = resolved["status_changes"]
    unchanged_count = resolved["unchanged_count"]
    unseen_rows = resolved["unseen_rows"]
    fetch_gap = None
    if searched > 0 and fetched < searched:
        fetch_gap = {"ids_searched": searched, "ids_fetched": fetched}
        print(
            f"[stage_write] WARNING fetched {fetched} of {searched} emails "
            "— some were not read"
        )

    has_changes = bool(
        new_rows or status_changes or unseen_rows or duplicates_in_batch or invalid_entries
    ) or fetch_gap is not None

    pending = {
        "entries": resolved["entries"],
        "updates": resolved["updates"],
        "rows_to_append": resolved["rows_to_append"],
        "last_data_row": resolved["last_data_row"],
        "staged_at": datetime.now(timezone.utc).isoformat(),
        "diff": {
            "new_rows": new_rows,
            "status_changes": status_changes,
            "unchanged_count": unchanged_count,
            "duplicates_in_batch": duplicates_in_batch,
            "unseen_rows": unseen_rows,
            "invalid_entries": invalid_entries,
        },
    }
    with open(_PENDING_WRITE_PATH, "w") as f:
        json.dump(pending, f, indent=2)

    print(
        f"[stage_write] {len(new_rows)} new, {len(status_changes)} status changes, "
        f"{unchanged_count} unchanged, {len(duplicates_in_batch)} duplicates in batch, "
        f"{len(unseen_rows)} existing rows not seen, {len(invalid_entries)} invalid entries"
    )

    return {
        "new_rows_count": len(new_rows),
        "status_changes_count": len(status_changes),
        "unchanged_count": unchanged_count,
        "new_rows": new_rows,
        "status_changes": status_changes,
        "duplicates_in_batch": duplicates_in_batch,
        "unseen_rows": unseen_rows,
        "invalid_entries": invalid_entries,
        "fetch_gap": fetch_gap,
        "has_changes": has_changes,
    }


def commit_write() -> dict:
    """Writes the plan staged by the last stage_write call to the Tracker
    sheet exactly as it was resolved and presented for confirmation, then
    deletes the staged file.

    Does NOT recompute the plan - it applies the updates and rows_to_append
    persisted by stage_write, so the write is guaranteed to match the diff the
    user approved even if the sheet has changed since staging.

    Returns:
        A dict with rows_added and rows_updated, or an error dict if nothing
        is currently staged or the staged write is stale.
    """
    if not os.path.exists(_PENDING_WRITE_PATH):
        return {"error": "No pending write found. Call stage_write before commit_write."}

    with open(_PENDING_WRITE_PATH) as f:
        pending = json.load(f)

    staged_at = datetime.fromisoformat(pending["staged_at"])
    age = datetime.now(timezone.utc) - staged_at
    if age > _STALE_PENDING_WRITE_AGE:
        return {"error": "The pending write is stale (staged more than 1 hour ago) and should be re-staged."}

    gmail_service = get_gmail_service()
    sheets_service = build("sheets", "v4", credentials=gmail_service._http.credentials)

    result = _apply(
        sheets_service,
        pending["updates"],
        pending["rows_to_append"],
        pending["last_data_row"],
    )

    os.remove(_PENDING_WRITE_PATH)

    return result

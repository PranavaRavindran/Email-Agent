import json
import os
import re
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build

from auth import get_gmail_service
from tools.find_application_date import find_application_date, is_confirmation_email
from tools.get_email_detail import get_email_detail, get_fetched_ids
from tools.search_email_ids import get_last_search_count, get_last_search_range

_SPREADSHEET_ID = "1NlX-ND9UIQkalliOqsw4eb9ItcMNllo-dkuEwWzvI3g"
_READ_RANGE = "Tracker!A3:F"
_FIRST_DATA_ROW = 3

_PENDING_WRITE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pending_write.json"
)

_RUN_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run_log.jsonl"
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


def _log_run(
    entries_received: int,
    new: int,
    status_changes: int,
    unchanged: int,
    duplicates_in_batch: int,
    unseen_rows: int,
    invalid_entries: int,
    ids_searched: int,
    ids_fetched: int,
    refused: bool,
) -> None:
    """Appends one drift-tracking JSON line to run_log.jsonl. Never raises -
    a logging failure must not affect stage_write's return value."""
    try:
        line = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "entries_received": entries_received,
            "new": new,
            "status_changes": status_changes,
            "unchanged": unchanged,
            "duplicates_in_batch": duplicates_in_batch,
            "unseen_rows": unseen_rows,
            "invalid_entries": invalid_entries,
            "ids_searched": ids_searched,
            "ids_fetched": ids_fetched,
            "refused": refused,
        }
        with open(_RUN_LOG_PATH, "a") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:
        print("[stage_write] WARNING could not write run_log.jsonl")


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


def _fetch_source_emails(entry: dict) -> list:
    """Fetches every email in entry's source_email_ids group, once. Missing
    or empty source_email_ids yields an empty list. Skips (and logs) any id
    that fails to fetch rather than raising, since a single unreadable email
    must not block resolution of the rest of the entry."""
    source_ids = entry.get("source_email_ids") or []
    emails = []
    for email_id in source_ids:
        try:
            emails.append(get_email_detail(email_id)["email"])
        except Exception as e:
            print(f"[resolve_dates] ERROR fetching {email_id}: {e}")
    return emails


def _has_observed_confirmation(emails: list) -> bool:
    """Determines whether an entry's group contains an application
    confirmation by observing the emails themselves, rather than trusting a
    reported flag: classifies each already-fetched email with
    is_confirmation_email. The group has a confirmation if any email does."""
    return any(
        is_confirmation_email(email.get("subject", ""), email.get("body", ""))
        for email in emails
    )


def _group_source_text(emails: list) -> str:
    """Concatenates subject+body of the entry's own already-fetched source
    emails, for find_application_date's source_text parameter. Used only for
    requisition/job identifier extraction on the sought side, so a weak
    match (no role identified) belonging to a different requisition at the
    same company is never accepted for this entry - see _role_matches and
    _outcome_role_matches in find_application_date.py."""
    return " ".join(f"{email.get('subject', '')} {email.get('body', '')}" for email in emails)


def _resolve_dates(entries: list) -> list:
    """For each entry lacking an observed confirmation email, attempts a
    backward lookback via find_application_date. Mutates nothing; returns a
    new list.

    Reads source_email_ids (list of Gmail message ids for the entry's
    group) to determine, by observation via _has_observed_confirmation,
    whether the group actually contains an application confirmation. This
    is never taken on the agent's word — a reported has_confirmation flag
    previously flickered between true and false for the identical
    application across runs, since it was a model judgement rather than
    something code verified.

    Also reads the OPTIONAL key date_approximate (bool): whether the date is
    a fallback.

    Entries with an observed confirmation pass through untouched. For every
    other entry, find_application_date is called with the entry's company,
    role, current date as before_date, and source_text: the group's own
    source emails (already fetched above to check for a confirmation, not
    fetched again) concatenated for find_application_date's requisition/job
    identifier extraction. A successful lookback replaces the date and
    clears date_approximate; a miss or an exception keeps the existing date
    and marks it approximate.
    """
    resolved = []
    for entry in entries:
        entry = dict(entry)

        source_emails = _fetch_source_emails(entry)

        if _has_observed_confirmation(source_emails):
            resolved.append(entry)
            continue

        try:
            result = find_application_date(
                company=entry["company"],
                role=entry["role"],
                before_date=entry["date"],
                source_text=_group_source_text(source_emails),
            )
        except Exception as e:
            entry["date_approximate"] = True
            print(f"[resolve_dates] ERROR {entry.get('company')}/{entry.get('role')}: {e}")
            resolved.append(entry)
            continue

        if result.get("found"):
            entry["date"] = result["date"]
            entry["date_approximate"] = False
        else:
            entry["date_approximate"] = True

        flag = "approximate" if entry["date_approximate"] else "confirmed"
        print(f"[resolve_dates] {entry['company']} / {entry['role']} -> {entry['date']} | {flag}")
        resolved.append(entry)

    return resolved


def _merge_duplicates(valid_entries: list) -> tuple:
    """Groups entries by normalized company+role and merges each group into a
    single entry: earliest date, latest status, longest company and role
    strings. Returns (deduped_entries, duplicates_in_batch).

    Grouping uses the same tolerance as sheet matching (_keys_match): role
    must match exactly, but company keys are grouped together when equal OR
    when one is a whitespace-boundary prefix of the other (via
    _company_matches) - a short/truncated company form (e.g. "CrossLink")
    must merge with the full form ("CrossLink Professional Tax Solutions")
    rather than sailing through as a separate new row. This is done as a
    second pass over the exact-key groups: exact grouping first, then any
    two groups whose roles match exactly and whose company keys satisfy
    _company_matches are unioned together.
    """
    groups = {}
    group_order = []
    for entry in valid_entries:
        key = (_normalize(entry["company"]), _normalize(entry["role"]))
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(entry)

    parent = {key: key for key in group_order}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(key_a, key_b):
        root_a, root_b = find(key_a), find(key_b)
        if root_a != root_b:
            parent[root_b] = root_a

    for i, key_a in enumerate(group_order):
        for key_b in group_order[i + 1:]:
            if key_a[1] == key_b[1] and _company_matches(key_a[0], key_b[0]):
                union(key_a, key_b)

    merged_groups = {}
    merged_order = []
    for key in group_order:
        root = find(key)
        if root not in merged_groups:
            merged_groups[root] = []
            merged_order.append(root)
        merged_groups[root].extend(groups[key])

    duplicates_in_batch = []
    deduped_entries = []
    for root in merged_order:
        group = merged_groups[root]
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


def preview_resolve(entries: list) -> dict:
    """Runs date resolution and duplicate merging on entries WITHOUT reading
    the sheet, computing a diff, or persisting anything. Returns the resolved,
    merged entries for display. This is the preview counterpart of
    stage_write's resolution step, so a preview shows the same dates and
    merges a stage would produce.

    Args:
        entries: List of dicts, each with keys: date, company, role, source,
            status, and optionally source_email_ids.

    Returns:
        A dict with 'entries': the resolved and merged list, each entry
        including a date_approximate flag, and 'merged_count': how many
        duplicate groups were merged.
    """
    print(f"[preview_resolve] resolving {len(entries)} entries")

    invalid_entries = []
    valid_entries = []
    for entry in entries:
        cleaned, reason = _validate_entry(entry)
        if reason is not None:
            invalid_entries.append({"entry": entry, "reason": reason})
        else:
            valid_entries.append(cleaned)

    resolved_entries = _resolve_dates(valid_entries)
    merged_entries, duplicates_in_batch = _merge_duplicates(resolved_entries)

    return {
        "entries": merged_entries,
        "merged_count": len(duplicates_in_batch),
        "invalid_entries": invalid_entries,
    }


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
        themselves), duplicates_in_batch, unseen_rows, invalid_entries, and a
        has_changes flag. If entries is empty, returns an error dict instead
        and does not write pending_write.json. If a search was run but fewer
        emails were fetched than were searched, or more entries were provided
        than emails were read, returns an error dict instead and stages
        nothing, since entries could not have been derived from email
        content.
    """
    if not entries:
        return {"error": "No entries were provided; stage_write requires at least one entry."}

    searched = get_last_search_count()
    fetched = len(get_fetched_ids())
    if searched > 0 and fetched < searched:
        print(f"[stage_write] REFUSED — {searched} ids searched, {fetched} emails read")
        _log_run(
            entries_received=len(entries),
            new=0,
            status_changes=0,
            unchanged=0,
            duplicates_in_batch=0,
            unseen_rows=0,
            invalid_entries=0,
            ids_searched=searched,
            ids_fetched=fetched,
            refused=True,
        )
        return {
            "error": (
                f"Only {fetched} of {searched} searched emails were read. Entries cannot "
                "be derived from emails that were not opened. Fetch every id returned by "
                "search_email_ids before staging."
            )
        }

    if searched > 0 and len(entries) > fetched:
        print(f"[stage_write] REFUSED — {len(entries)} entries from {fetched} emails read")
        _log_run(
            entries_received=len(entries),
            new=0,
            status_changes=0,
            unchanged=0,
            duplicates_in_batch=0,
            unseen_rows=0,
            invalid_entries=0,
            ids_searched=searched,
            ids_fetched=fetched,
            refused=True,
        )
        return {
            "error": (
                f"Received {len(entries)} entries but only {fetched} emails were read. "
                "Entries cannot outnumber the emails they came from."
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

    # Runs after the range check above, which validates each entry's original
    # date. A date corrected here by backward lookback may legitimately fall
    # before that range, so it must not be re-validated against it.
    valid_entries = _resolve_dates(valid_entries)
    for entry in valid_entries:
        entry.pop("source_email_ids", None)
        entry.pop("date_approximate", None)

    deduped_entries, duplicates_in_batch = _merge_duplicates(valid_entries)

    resolved = _resolve(deduped_entries)

    new_rows = resolved["new_rows"]
    status_changes = resolved["status_changes"]
    unchanged_count = resolved["unchanged_count"]
    unseen_rows = resolved["unseen_rows"]

    has_changes = bool(
        new_rows or status_changes or unseen_rows or duplicates_in_batch or invalid_entries
    )

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

    _log_run(
        entries_received=len(entries),
        new=len(new_rows),
        status_changes=len(status_changes),
        unchanged=unchanged_count,
        duplicates_in_batch=len(duplicates_in_batch),
        unseen_rows=len(unseen_rows),
        invalid_entries=len(invalid_entries),
        ids_searched=searched,
        ids_fetched=fetched,
        refused=False,
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

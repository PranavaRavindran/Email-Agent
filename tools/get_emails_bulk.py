import concurrent.futures

from tools.get_email_detail import _BODY_CACHE, _fetch_one

_MAX_WORKERS = 8


def get_emails_bulk(email_ids: list[str]) -> dict:
    """Fetches the full content of multiple emails in one call.

    Cached ids are returned immediately without re-fetching. Ids not yet
    cached are fetched concurrently via a thread pool, since fetching each
    of a search result's ~48 ids one at a time in a loop is the mechanism
    behind this project's prior fabrication incidents - every individual
    call was a point where extraction could stop early. A single call here
    removes that per-call decision point.

    Args:
        email_ids: Gmail message ids to fetch, typically the full list
            returned by search_email_ids.

    Returns:
        A dict keyed by email_id. Each value is either the same shape
        get_email_detail returns for a single email ({"email": {...}}), or
        {"error": "<message>"} if that particular id failed to fetch. A
        failed id does not abort the rest of the batch.
    """
    email_ids = list(dict.fromkeys(email_ids))
    results = {}
    cached_ids = [email_id for email_id in email_ids if email_id in _BODY_CACHE]
    uncached_ids = [email_id for email_id in email_ids if email_id not in _BODY_CACHE]

    for email_id in cached_ids:
        results[email_id] = _fetch_one(email_id, quiet=True)

    error_count = 0
    if uncached_ids:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(_MAX_WORKERS, len(uncached_ids))
        ) as executor:
            future_to_id = {
                executor.submit(_fetch_one, email_id, True): email_id for email_id in uncached_ids
            }
            for future in concurrent.futures.as_completed(future_to_id):
                email_id = future_to_id[future]
                try:
                    results[email_id] = future.result()
                except Exception as e:
                    error_count += 1
                    results[email_id] = {"error": f"{type(e).__name__}: {e}"}

    new_count = len(uncached_ids) - error_count
    fetched = len(cached_ids) + new_count
    print(
        f"[get_emails_bulk] fetched {fetched} of {len(email_ids)} requested "
        f"({len(cached_ids)} cached, {new_count} new, {error_count} errors)"
    )
    return results

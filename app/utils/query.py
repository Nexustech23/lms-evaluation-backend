# ============================================================
# Safe helpers for building MongoDB queries from untrusted user input.
# ============================================================
import re

# A search term comes from a UI text box, not from anywhere that needs a
# 5000-character pattern. Long inputs are truncated (not rejected) so the
# search box never surfaces an error.
_MAX_SEARCH_LEN = 100


def search_regex(term: str | None) -> dict | None:
    """
    Build a safe case-insensitive MongoDB ``$regex`` clause from free user
    text.

    ``re.escape()`` neutralises every regex metacharacter, so a crafted
    input like ``(.*a){30}$`` or a leading ``.*`` is matched *literally*
    instead of triggering catastrophic backtracking / forcing a full
    collection scan (a cheap denial-of-service otherwise).

    Returns ``None`` for empty/blank input so callers can simply skip
    adding the clause::

        regex = search_regex(search)
        if regex:
            query["$or"] = [{"name": regex}, {"code": regex}]
    """
    if not term:
        return None
    term = term.strip()[:_MAX_SEARCH_LEN]
    if not term:
        return None
    return {"$regex": re.escape(term), "$options": "i"}

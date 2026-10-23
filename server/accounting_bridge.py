"""Optional bridge into the local Accounting app's own normalize/parse/db functions.

This never duplicates Accounting's parsing logic: it imports the real modules from
ACCOUNTING_DIR and calls them directly, same as the Accounting web UI's own buttons do.
If that project isn't where expected (drive unmounted, moved, etc.), accounting mode is
simply unavailable -- the rest of the talker service is unaffected.
"""
import os
import re
import sys
from datetime import date
from pathlib import Path

ACCOUNTING_DIR = Path(os.environ.get(
    "TALKER_ACCOUNTING_DIR",
    "/run/media/nomad/SSDKingston240/Ubuntu_bak/AI/Accounting",
))

AVAILABLE = False
IMPORT_ERROR = None

try:
    if str(ACCOUNTING_DIR) not in sys.path:
        sys.path.insert(0, str(ACCOUNTING_DIR))
    from normalizer import DEFAULT_CATEGORIES as _DEFAULT_CATEGORIES, normalize_text as _normalize_text
    from parser import ExpenseParser, ParseStatus, parse_accounting_text as _parse_accounting_text
    import database as _db
    AVAILABLE = True
except ImportError as e:
    IMPORT_ERROR = str(e)

_CATEGORY_LINE = re.compile(r"^.+[：:].+$")


def _has_date_line(text):
    return any(ExpenseParser.DATE_PATTERN.match(line.strip())
               for line in text.splitlines() if line.strip())


def looks_like_accounting(text):
    """Strict gate so ordinary chat never gets misfiled as a memo: only a date line
    (1/19 or 1/19（一）) or a 類別：人名 line counts -- neither shows up by accident."""
    if not AVAILABLE:
        return False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ExpenseParser.DATE_PATTERN.match(line) or _CATEGORY_LINE.match(line):
            return True
    return False


def _normalize_options():
    """Same {names, categories} the Accounting UI builds before every '來點魔法' call --
    without it the normalizer doesn't recognize this user's own people and treats their
    real names as unknown text (confirmed by testing against a throwaway copy of the db)."""
    names = []
    for p in _db.get_all_persons():
        names.append(p["name"])
        if p["aliases"]:
            names.extend(a.strip() for a in p["aliases"].split(",") if a.strip())
    names.extend(_db.get_person_proxy_mappings().keys())
    categories = list(dict.fromkeys([*_DEFAULT_CATEGORIES, *_db.get_all_category_prices().keys()]))
    return {"names": names, "categories": categories}


def ingest(text):
    """Normalize -> parse -> write, mirroring Accounting's own '一次寫入資料庫' button.
    Returns a summary dict for the toast: {"recorded", "pending", "memos"}, or
    {"error": "..."} if something went wrong."""
    if not AVAILABLE:
        return {"error": IMPORT_ERROR or "accounting app not available"}

    target_date = None if _has_date_line(text) else date.today().isoformat()

    try:
        normalized = _normalize_text(text, **_normalize_options()).text
    except Exception:
        normalized = text  # best-effort cleanup only; parsing still works on raw text

    try:
        result = _parse_accounting_text(normalized, target_date=target_date)

        recorded = 0
        pending = 0
        for exp in result.expenses:
            if exp.status != ParseStatus.SUCCESS:
                continue
            # Same date+amount already on record: don't silently double-book, queue it
            # for review like the UI does when it hits a conflict.
            if _db.find_expenses_by_date_and_amount(exp.date, exp.amount):
                _db.add_pending_review(
                    raw_text=exp.original_text or f"{exp.category}：{exp.person}+{exp.amount}",
                    source_line=exp.original_text or exp.category,
                    date=exp.date,
                )
                pending += 1
            else:
                _db.add_expense(date=exp.date, category=exp.category, person=exp.person,
                                 amount=exp.amount, quantity=exp.quantity, note=exp.note)
                recorded += 1

        for item in result.pending:
            _db.add_pending_review(raw_text=item["raw_text"], source_line=item["source_line"],
                                    date=item["date"])
            pending += 1

        memos = sum(1 for m in result.memos if _db.add_memo(m.date, m.content, m.person) != -1)

        # Parser already has an identical date+category+person on record -- correct to skip,
        # but must be reported, not silently dropped (the UI has a human there to notice; we don't).
        skipped = len(result.duplicates)

        return {"recorded": recorded, "pending": pending, "memos": memos, "skipped": skipped}
    except Exception as e:
        return {"error": str(e)}

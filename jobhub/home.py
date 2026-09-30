"""What the home dashboard shows.

Five cards, one query pass. Nothing here spends tokens and nothing writes except the
`last_visit` stamp, which is what makes "new since you were last here" mean anything.
Kept out of web/app.py for the same reason pipeline.py is: it is the interesting half
and it is testable without a request context.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import config, db, prefilter, roletype

#: How far ahead the deadlines card looks. Beyond this a posting is not urgent yet.
DEADLINE_WINDOW_DAYS = 14

#: Loads closer together than this are one visit. Without it, "since your last visit" would
#: mean "since you last refreshed" — the window collapses to nothing and the card reads 0
#: forever, even right after a run brought in thousands of postings.
SESSION_GAP_MINUTES = 45

#: Cards show a handful of rows; the rest live on their own page.
TOP_N = 4

_ACTIVE_STATUSES = ("shortlisted", "applied", "interview", "offer")
_STAGE_LABELS = [("shortlisted", "To apply"), ("applied", "Applied"),
                 ("interview", "Interview"), ("offer", "Offer")]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _meta_time(conn, key: str) -> datetime | None:
    raw = db.get_meta(conn, key)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def last_visit(conn) -> datetime | None:
    """The cutoff "new since" is measured from: the end of the previous *session*, not the last page load."""
    return _meta_time(conn, "last_visit")


def stamp_visit(conn) -> None:
    """Advance the cutoff only when a genuinely new session starts.

    `seen_at` is touched on every load; `last_visit` only moves forward when the gap since the
    previous load exceeds SESSION_GAP_MINUTES, and it moves to the *end of that previous session*.
    Refreshing therefore keeps showing the same arrivals instead of zeroing the card.
    """
    now = _now()
    seen = _meta_time(conn, "seen_at")
    if seen is None:
        db.set_meta(conn, "last_visit", now.isoformat(timespec="seconds"))
    elif (now - seen) > timedelta(minutes=SESSION_GAP_MINUTES):
        db.set_meta(conn, "last_visit", seen.isoformat(timespec="seconds"))
    db.set_meta(conn, "seen_at", now.isoformat(timespec="seconds"))
    conn.commit()   # db.set_meta leaves the transaction open; closing without this would discard it


def _iso_date(value: str | None) -> datetime | None:
    """Timestamps in the DB are a mix of naive dates ('2026-10-01') and aware stamps; normalise to aware UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _since_label(dt: datetime | None) -> str:
    if dt is None:
        return "your first visit"
    days = (_now() - dt).days
    if days <= 0:
        return f"earlier today, {dt.strftime('%H:%M')}"
    if days == 1:
        return f"yesterday, {dt.strftime('%H:%M')}"
    if days < 7:
        return f"{dt.strftime('%A')}, {dt.strftime('%H:%M')}"
    return dt.strftime("%-d %b")


def _summarise_profile(profile) -> dict[str, Any]:
    """Only what the profile actually holds — no invented fields."""
    terms = " / ".join([profile.term] + list(profile.term_also_accept)) if profile.term else "no term set"
    alt = " / ".join(profile.alt_terms)
    d = profile.dealbreakers
    n_breakers = sum(len(getattr(d, f) or []) for f in
                     ("unpaid", "clearance", "citizenship", "level", "location_exclude", "other"))
    edited = None
    if config.PROFILE_YAML.exists():
        edited = datetime.fromtimestamp(config.PROFILE_YAML.stat().st_mtime).strftime("%-d %b")
    roles = profile.target_role_labels()
    excluded = [roletype.label(k) for k in profile.excluded_role_types]
    return {
        "term": terms,
        "alt_term": alt,
        "locations": list(profile.locations),
        "remote_ok": profile.remote_ok,
        # Parenthetical notes belong on Profile, not in this space-constrained overview.  For example,
        # "Japan (citizenship held; passport may need renewal)" becomes the still-accurate "Japan" here.
        "work_authorization": [str(x).split(" (", 1)[0] for x in profile.work_authorization],
        "roles": roles[:4],
        "role_count": len(roles),
        "excluded": excluded,
        "excluded_count": len(excluded),
        "dealbreakers": n_breakers,
        "edited": edited,
    }


def home_data(conn, *, rows: list[dict], stats: dict, profile=None) -> dict[str, Any]:
    """`rows` are the same evaluation rows the jobs page builds, so the two can never disagree."""
    profile = profile or config.load_profile()
    since = last_visit(conn)
    window = _now() + timedelta(days=DEADLINE_WINDOW_DAYS)

    live = [r for r in rows if r["bucket"] in ("likely", "reach", "wildcard")
            and not r.get("hard_reject_reason")]
    unreviewed = [r for r in live if r["app_status"] == "new"]

    fresh = unreviewed
    if since is not None:
        cutoff = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        fresh = [r for r in unreviewed
                 if (d := _iso_date(r.get("first_seen_at"))) and d > cutoff]

    score = lambda r: (r["likelihood"] or 0, r["desirability"] or 0)
    fresh_sorted = sorted(fresh, key=score, reverse=True)
    split = {b: sum(1 for r in fresh if r["bucket"] == b) for b in ("likely", "reach", "wildcard")}

    buckets = [(b, sum(1 for r in rows if r["bucket"] == b and not r.get("hard_reject_reason")))
               for b in ("likely", "reach", "wildcard", "archive")]
    open_now = sum(n for b, n in buckets if b in ("likely", "reach"))

    counts = dict(conn.execute(
        "SELECT status, COUNT(*) FROM applications GROUP BY status").fetchall())
    stages = [{"key": k, "name": label, "n": counts.get(k, 0)} for k, label in _STAGE_LABELS]
    in_flight = sum(counts.get(k, 0) for k in _ACTIVE_STATUSES)

    deadlines = []
    for r in live:
        due = _iso_date(r.get("deadline"))
        if due is None or due > window:
            continue
        days = (due.date() - _now().date()).days
        if days < 0:
            continue
        deadlines.append({
            "days": days, "date": due, "mon": due.strftime("%b"), "d": due.strftime("%-d"),
            "company": r["company_name"], "title": r["title"], "job_id": r["job_id"],
            "state": {"new": "not reviewed", "shortlisted": "shortlisted, not applied"}.get(
                r["app_status"], r["app_status"]),
            "urgent": days <= 5,
        })
    deadlines.sort(key=lambda d: d["days"])

    return {
        "since": since,
        "since_label": _since_label(since),
        "new_count": len(fresh),
        "new_split": split,
        "new_top": fresh_sorted[:TOP_N],
        "unreviewed": len(unreviewed),
        "open_now": open_now,
        "buckets": buckets,
        "bucket_max": max([n for _, n in buckets if n] or [1]),
        "top_unreviewed": sorted(unreviewed, key=score, reverse=True)[:TOP_N],
        "stages": stages,
        "in_flight": in_flight,
        "needs_you": _needs_you(conn, counts),
        "deadlines": deadlines[:TOP_N],
        "deadline_total": len(deadlines),
        "profile": _summarise_profile(profile),
        "stats": stats,
    }


def _needs_you(conn, counts: dict[str, int]) -> list[dict[str, str]]:
    """Applications that are waiting on you rather than on them."""
    out: list[dict[str, str]] = []
    rows = conn.execute(
        "SELECT j.company_name, j.title, a.status, a.updated_at FROM applications a "
        "JOIN jobs j ON j.id = a.job_id WHERE a.status IN ('interview', 'offer') "
        "ORDER BY a.updated_at DESC LIMIT 3").fetchall()
    for r in rows:
        out.append({"company": r["company_name"],
                    "what": "interview scheduled" if r["status"] == "interview" else "offer to answer",
                    "when": (r["updated_at"] or "")[:10]})
    return out

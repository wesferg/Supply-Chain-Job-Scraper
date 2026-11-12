"""ATS adapter registry.

Every fetcher: (name, ids, title_ok, loc_ok, search_terms, known) -> list[Job]
and raises models.BoardUnavailable when the board itself is gone or wrong.
"""
from . import workday, simple, eightfold

FETCHERS = {
    "workday": lambda name, c, title_ok, loc_ok, search_terms, known: workday.fetch(
        name, c, search_terms, title_ok, known, loc_ok),
    "greenhouse": lambda name, c, title_ok, loc_ok, search_terms, known: simple.greenhouse(
        name, c, title_ok, loc_ok, known),
    "lever": lambda name, c, title_ok, loc_ok, search_terms, known: simple.lever(
        name, c, title_ok, loc_ok, known),
    "ashby": lambda name, c, title_ok, loc_ok, search_terms, known: simple.ashby(
        name, c, title_ok, loc_ok, known),
    "smartrecruiters": lambda name, c, title_ok, loc_ok, search_terms, known: simple.smartrecruiters(
        name, c, title_ok, loc_ok, known),
    "eightfold": lambda name, c, title_ok, loc_ok, search_terms, known: eightfold.fetch(
        name, c, search_terms, title_ok, known, loc_ok),
}

ID_KEYS = {
    "workday": ("host", "tenant", "site"),
    "greenhouse": ("token",),
    "lever": ("slug",),
    "ashby": ("org",),
    "smartrecruiters": ("company_id",),
    "eightfold": ("host", "domain"),
}


def board_key(ats: str, ids: dict) -> str:
    """One stable id per job board, used to de-duplicate curated vs discovered boards."""
    if ats == "workday":
        return f"workday:{ids['host']}/{ids['site']}".lower()
    return f"{ats}:{ids[ID_KEYS[ats][0]]}".lower()

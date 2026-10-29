"""Local substitute for branch protection. Never merges or pushes."""

from __future__ import annotations


class MergeRefused(RuntimeError):
    pass


def _check_ok(item: dict[str, object]) -> bool:
    status = item.get("status")
    if status not in (None, "COMPLETED"):
        return False
    conclusion = item.get("conclusion")
    if conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
        return True
    state = item.get("state")
    return state in {"SUCCESS", "NEUTRAL", "SKIPPED"}


def evaluate_merge(payload: dict[str, object], *, target: str) -> dict[str, object]:
    if target != "master":
        raise MergeRefused(f"refusing target {target}")
    reviews = payload.get("reviews") or []
    if not isinstance(reviews, list):
        raise MergeRefused("human approval missing")
    latest: dict[str, str] = {}
    for review in reviews:
        if not isinstance(review, dict):
            continue
        author = review.get("author") or {}
        login = ""
        if isinstance(author, dict):
            login = str(author.get("login") or "")
        state = str(review.get("state") or "")
        if login:
            latest[login] = state
    approved = any(state == "APPROVED" for state in latest.values())
    rollup = payload.get("statusCheckRollup") or []
    if not isinstance(rollup, list) or not rollup:
        raise MergeRefused("ci has not passed")
    if not all(isinstance(item, dict) and _check_ok(item) for item in rollup):
        raise MergeRefused("ci has not passed")
    if not approved:
        raise MergeRefused("human approval missing")
    return {"proceed": True, "merged": False, "target": target}

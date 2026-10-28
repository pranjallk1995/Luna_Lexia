"""Score old.jsonl vs new.jsonl: grounding, coverage, identifier validity, flags, failures, time."""

import json
import re
import sys
from datetime import date
from pathlib import Path
from statistics import mean, median

sys.path.insert(0, sys.argv[2] if len(sys.argv) > 2 else "")  # code dir, for IDENTIFIER_PATTERNS
from app.core.schemas import IDENTIFIER_PATTERNS  # noqa: E402

FAILED = "Details could not be extracted"
FALLBACK = "Extraction from the text failed"
SKIP_FIELDS = {"currency"}


def norm(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.lower())


def number_forms(value: float) -> list[str]:
    forms = {f"{value:.2f}", f"{value:.0f}" if value == int(value) else f"{value}"}
    return [norm(f) for f in forms]


def date_forms(value: str) -> list[str]:
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return [norm(value)]
    month = d.strftime("%b").lower()
    full = d.strftime("%B").lower()
    return [
        norm(x)
        for x in (
            f"{d.day:02d}{d.month:02d}{d.year}",
            f"{d.day}{d.month}{d.year}",
            f"{d.day:02d}{d.month:02d}{d.year % 100:02d}",
            f"{d.year}{d.month:02d}{d.day:02d}",
            f"{d.day:02d}{month}{d.year}",
            f"{d.day}{month}{d.year}",
            f"{d.day:02d}{full}{d.year}",
            f"{d.day}{full}{d.year}",
            f"{full}{d.day}{d.year}",
            f"{month}{d.day}{d.year}",
            f"{d.day:02d}{month}{d.year % 100:02d}",
        )
    ]


def leaves(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from leaves(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from leaves(v, f"{path}[{i}]")
    else:
        yield path, obj


def grounded(field: str, value, text: str) -> bool | None:
    """True/False when checkable against the parsed text, None when not checkable."""
    if value is None or isinstance(value, bool) or field.split(".")[-1] in SKIP_FIELDS:
        return None
    if isinstance(value, (int, float)):
        if value == 0:
            return None
        return any(f and f in text for f in number_forms(float(value)))
    value = str(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return any(f in text for f in date_forms(value))
    if re.fullmatch(r"\d{4}-\d{2}", value):  # financial / assessment year: 2024-25
        start = value[:4]
        return start in text
    n = norm(value)
    if len(n) < 3:
        return None
    if n in text:
        return True
    # long free text (addresses, descriptions): count as grounded when most words appear
    words = [norm(w) for w in value.split() if len(norm(w)) >= 3]
    return len(words) >= 3 and sum(w in text for w in words) / len(words) >= 0.8


def bad_identifiers(extracted: dict) -> list[str]:
    bad = []
    for field, value in leaves(extracted):
        name = field.split(".")[-1].split("[")[0]
        if not isinstance(value, str) or "*" in value or "XXXX" in value:
            continue
        for suffix, (pattern, label) in IDENTIFIER_PATTERNS.items():
            if (name == suffix or name.endswith(f"_{suffix}")) and not pattern.fullmatch(value):
                bad.append(f"{field}={value}")
    return bad


def score(row: dict) -> dict:
    if "crash" in row:
        return {"crash": True}
    text = norm(" ".join(row.get("page_texts") or []))
    checks, filled, total, not_in_text = [], 0, 0, []
    for field, value in leaves(row["extracted"]):
        total += 1
        if value not in (None, "", []):
            filled += 1
        g = grounded(field, value, text) if text else None
        if g is not None:
            checks.append(g)
            if not g:
                not_in_text.append(f"{field}={value}")
    flags = row["review_flags"]
    return {
        "crash": False,
        "failed": any(f.startswith(FAILED) for f in flags),
        "fallback": any(f.startswith(FALLBACK) for f in flags),
        "flags": len(flags),
        "filled": filled,
        "fields": total,
        "checked": len(checks),
        "grounded": sum(checks),
        "not_in_text": not_in_text,
        "bad_ids": bad_identifiers(row["extracted"]),
        "seconds": row["seconds"],
    }


def load(path: str) -> dict:
    return {r["name"]: r for r in map(json.loads, Path(path).read_text().splitlines())}


def main() -> None:
    base = Path(sys.argv[1])
    runs = {v: load(base / f"{v}.jsonl") for v in ("old", "new")}
    names = sorted(set(runs["old"]) & set(runs["new"]))
    scored = {v: {n: score(runs[v][n]) for n in names} for v in runs}
    summary = {}
    for v, s in scored.items():
        ok = [x for x in s.values() if not x["crash"]]
        checked = sum(x["checked"] for x in ok)
        summary[v] = {
            "documents": len(s),
            "crashes": sum(x["crash"] for x in s.values()),
            "extraction_failed": sum(x["failed"] for x in ok),
            "image_fallback": sum(x["fallback"] for x in ok),
            "docs_with_flags": sum(x["flags"] > 0 for x in ok),
            "total_flags": sum(x["flags"] for x in ok),
            "fill_rate": round(sum(x["filled"] for x in ok) / max(1, sum(x["fields"] for x in ok)), 3),
            "values_checked": checked,
            "grounding_rate": round(sum(x["grounded"] for x in ok) / max(1, checked), 3),
            "ungrounded_values": checked - sum(x["grounded"] for x in ok),
            "bad_identifier_docs": sum(bool(x["bad_ids"]) for x in ok),
            "bad_identifiers": sum(len(x["bad_ids"]) for x in ok),
            "mean_seconds": round(mean(x["seconds"] for x in s.values()), 1),
            "median_seconds": round(median(x["seconds"] for x in s.values()), 1),
            "total_minutes": round(sum(x["seconds"] for x in s.values()) / 60, 1),
        }
    per_doc = []
    for n in names:
        o, w = scored["old"][n], scored["new"][n]
        per_doc.append({"name": n, "type": runs["new"][n].get("doc_type") or runs["new"][n]["hint"], "old": o, "new": w})
    (base / "scores.json").write_text(json.dumps({"summary": summary, "per_doc": per_doc}, indent=1, default=str))
    print(json.dumps(summary, indent=1))


main()

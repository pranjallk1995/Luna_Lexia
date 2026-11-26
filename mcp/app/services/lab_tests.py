"""What each lab test measures: explained once per test, and shown beside its results in Luna Vita.

The local model writes an explanation the first time a stored health report has a test
(in the background, so an upload never waits for it). An assistant can write better ones
through MCP; the model never replaces those. Tests match across reports and labs by
``test_key``, as Luna Vita matches them for trends.
"""

from __future__ import annotations

import contextvars
import logging
import re
import threading
from typing import Any

from app.core.schemas import clean_text, test_key

logger = logging.getLogger(__name__)

HEALTH_REPORT = "health_report"
ABOUT_MAX_CHARS = 400
# Test names per model call: a full-body checkup has about 90.
BATCH = 15
MAX_SAVE = 200


def tidy(text: Any) -> str:
    """Collapse whitespace; unlike clean_text, never cut the text short."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


class LabTestService:
    """Explanations of lab tests, one per test key, for the current person."""

    def __init__(self, repository: Any, documents: Any, explainer: Any = None) -> None:
        self.repository = repository
        self.documents = documents
        self.explainer = explainer
        # One model run at a time: two uploads in a row would otherwise explain the same tests twice.
        self._running = threading.Lock()

    @staticmethod
    def names_of(results: list[dict[str, Any]]) -> dict[str, str]:
        """Each test key with the first name printed for it."""
        names: dict[str, str] = {}
        for result in results:
            name = clean_text((result or {}).get("test_name"))
            key = test_key(name)
            if key and key not in names:
                names[key] = name
        return names

    def explain_new(self, results: list[dict[str, Any]]) -> int:
        """Have the model explain every test here without an explanation; return how many it saved."""
        if self.explainer is None:
            return 0
        with self._running:
            names = self.names_of(results)
            known = self.repository.known(list(names))
            missing = [name for key, name in names.items() if key not in known]
            saved = 0
            for start in range(0, len(missing), BATCH):
                written = self.explainer.explain(missing[start : start + BATCH])
                rows = [(test_key(name), name, tidy(about)[:ABOUT_MAX_CHARS]) for name, about in written.items()]
                saved += self.repository.save(rows, "model")
            return saved

    def _explain_quietly(self, results: list[dict[str, Any]]) -> None:
        try:
            saved = self.explain_new(results)
            if saved:
                logger.info("Explained %d new lab tests.", saved)
        except Exception:  # noqa: BLE001 - explanations are optional; the report is stored either way
            logger.exception("Could not explain lab tests.")

    def explain_in_background(self, results: list[dict[str, Any]]) -> None:
        """Explain a stored report's new tests on a thread that acts for the same person."""
        if self.explainer is None or not results:
            return
        context = contextvars.copy_context()
        threading.Thread(
            target=context.run, args=(self._explain_quietly, list(results)), name="lexia-lab-tests", daemon=True
        ).start()

    def stored_results(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Every visible health report with each of its results, newest report first."""
        return [
            (document, result)
            for document in self.documents.list(doc_types=[HEALTH_REPORT])
            for result in (document.get("extracted") or {}).get("results") or []
        ]

    def explain_stored_in_background(self) -> None:
        """Explain the tests of reports stored before explanations existed."""
        self.explain_in_background([result for _, result in self.stored_results()])

    def explanations(self) -> dict[str, Any]:
        """Every explanation by test key, for Luna Vita."""
        rows = self.repository.all()
        return {
            "tests": {
                row["test_key"]: {"test_name": row["test_name"], "about": row["about"], "source": row["source"]}
                for row in rows
            },
            "count": len(rows),
        }

    def listing(self, missing_only: bool = False) -> dict[str, Any]:
        """Every test in the stored health reports, with its explanation and who wrote it."""
        explained = {row["test_key"]: row for row in self.repository.all()}
        tests: dict[str, dict[str, Any]] = {}
        for document, result in self.stored_results():
            name = clean_text(result.get("test_name"))
            key = test_key(name)
            if not key:
                continue
            entry = tests.setdefault(key, {"test_name": name, "panel": result.get("panel"), "reports": set()})
            entry["reports"].add(document["name"])
        listed = []
        for key, entry in tests.items():
            row = explained.get(key)
            if missing_only and row is not None:
                continue
            listed.append({
                "test_name": entry["test_name"],
                "panel": entry["panel"],
                "report_count": len(entry["reports"]),
                "about": row["about"] if row else None,
                "source": row["source"] if row else None,
            })
        listed.sort(key=lambda test: ((test["panel"] or "~").casefold(), test["test_name"].casefold()))
        return {"tests": listed, "count": len(listed), "missing": sum(test["about"] is None for test in listed)}

    def save(self, explanations: list[dict[str, Any]]) -> dict[str, Any]:
        """Store an assistant's explanations, replacing the model's or earlier ones for the same tests."""
        if not isinstance(explanations, list) or not explanations:
            raise ValueError("Give at least one explanation: {test_name, about}.")
        if len(explanations) > MAX_SAVE:
            raise ValueError(f"Give at most {MAX_SAVE} explanations at a time.")
        rows: dict[str, tuple[str, str, str]] = {}
        for item in explanations:
            name = clean_text((item or {}).get("test_name"))
            about = tidy((item or {}).get("about"))
            key = test_key(name)
            if not key or not about:
                raise ValueError("Every explanation needs a test_name and an about text.")
            if len(about) > ABOUT_MAX_CHARS:
                raise ValueError(f"The explanation of {name} is longer than {ABOUT_MAX_CHARS} characters; keep it to two sentences.")
            rows[key] = (key, name, about)
        saved = self.repository.save(list(rows.values()), "assistant")
        return {"status": "explanations_saved", "saved": saved}

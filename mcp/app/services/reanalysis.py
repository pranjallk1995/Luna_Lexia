"""Analyze stored documents again in the background, for example after a model change.

A run selects files whose records are missing, out of date (another model or
schema version), or changed on disk, then re-runs the analysis pipeline on each
in turn, keeping its stored document type as the hint. One run goes at a time
and can be stopped between documents. Nothing is kept about a run besides its
progress: selection is by model and schema, so starting again after a restart
or a stop picks up exactly the documents still left.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.core.schemas import document_type

if TYPE_CHECKING:
    from app.services.application import DocumentIndexer

logger = logging.getLogger(__name__)


class Reanalysis:
    """One background re-analysis run at a time, with its progress."""

    def __init__(self, indexer: DocumentIndexer) -> None:
        self.indexer = indexer
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._progress: dict[str, Any] = {"state": "idle"}

    @property
    def running(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def preview(
        self, names: list[str] | None = None, doc_types: list[str] | None = None, include_current: bool = False
    ) -> dict[str, Any]:
        """The documents a run with these filters would analyze, without starting it."""
        for doc_type in doc_types or []:
            document_type(doc_type)
        selected = self.indexer.outdated(names, doc_types, include_current)
        return {"model": self.indexer.analyzer.model, "count": len(selected), "documents": selected}

    def start(
        self, names: list[str] | None = None, doc_types: list[str] | None = None, include_current: bool = False
    ) -> dict[str, Any]:
        with self._lock:
            if self.running:
                raise ValueError("A re-analysis is already running; stop it or wait for it to finish.")
            selected = self.preview(names, doc_types, include_current)["documents"]
            self._stop.clear()
            self._progress = {
                "state": "running",
                "model": self.indexer.analyzer.model,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
                "total": len(selected),
                "done": 0,
                "current": None,
                "reanalyzed": [],
                "failed": [],
                "flagged": [],
            }
            self._worker = threading.Thread(target=self.run, args=(selected,), name="lexia-reanalysis", daemon=True)
            self._worker.start()
            return self.status()

    def run(self, selected: list[dict[str, Any]]) -> None:
        """Analyze each selected document; a failure is recorded and the run continues."""
        progress = self._progress
        for item in selected:
            if self._stop.is_set():
                progress["state"] = "stopped"
                break
            progress["current"] = item["name"]
            try:
                row = self.indexer.reanalyze(item["name"], item["doc_type"])
            except Exception as error:  # noqa: BLE001 - one bad file must not end the run
                logger.warning("Could not re-analyze %s: %s", item["name"], error)
                progress["failed"].append({"name": item["name"], "error": str(error)[:500]})
            else:
                progress["reanalyzed"].append(item["name"])
                if row.get("review_flags"):
                    progress["flagged"].append({"name": item["name"], "review_flags": row["review_flags"]})
            progress["done"] += 1
        else:
            progress["state"] = "finished"
        progress["current"] = None
        progress["finished_at"] = datetime.now(timezone.utc).isoformat()

    def stop(self) -> dict[str, Any]:
        """Stop after the document being analyzed now."""
        if self.running:
            self._stop.set()
            self._progress["state"] = "stopping"
        return self.status()

    def status(self) -> dict[str, Any]:
        progress = dict(self._progress)
        for key in ("reanalyzed", "failed", "flagged"):
            if key in progress:
                progress[key] = list(progress[key])
        if progress["state"] != "idle":
            progress["remaining"] = progress["total"] - progress["done"]
        return progress

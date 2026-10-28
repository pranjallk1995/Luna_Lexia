"""Run one Lexia pipeline variant over every stored document; write JSONL, never touch the DB.

usage: python run.py <variant: old|new> <model> <out.jsonl>
The variant's code must be on sys.path (PYTHONPATH) before this runs.
"""

import json
import os
import sys
import time
import traceback
from pathlib import Path

import psycopg

from app.core.config import AppConfig
from app.core.models import MetadataRules
from app.integrations.extraction import DocumentAnalyzer
from app.integrations.ollama import OllamaChat
from app.integrations.parsing import DoclingParser
from app.services.documents import DocumentReader
from app.services.pipeline import DocumentPipeline

variant, model, out_path = sys.argv[1], sys.argv[2], Path(sys.argv[3])
config = AppConfig.from_env()
reader = DocumentReader()
chat = OllamaChat(config.ollama_url, model, config.context_tokens)
args = [chat, reader, config.max_prompt_chars, config.max_vision_pages]
if variant == "new":
    args.append(True)
analyzer = DocumentAnalyzer(*args)
pipeline = DocumentPipeline(
    DoclingParser(reader, config.max_stored_text_chars, config.docling_device),
    analyzer,
    MetadataRules(config.max_tags, config.search_stop_words),
)

with psycopg.connect(config.database_url) as conn:
    stored = dict(conn.execute("SELECT name, doc_type FROM document_metadata").fetchall())

done = set()
if out_path.exists():
    done = {json.loads(line)["name"] for line in out_path.read_text().splitlines() if line.strip()}

doc_dir = Path(os.environ["DOCUMENT_DIR"])
names = sorted(n for n in stored if (doc_dir / n).exists())
with out_path.open("a") as out:
    for i, name in enumerate(names, 1):
        if name in done:
            continue
        data = (doc_dir / name).read_bytes()
        started = time.monotonic()
        row = {"name": name, "variant": variant, "model": model, "hint": stored[name]}
        try:
            analysis, content = pipeline.run(data, stored[name])
            row.update(
                doc_type=analysis.doc_type,
                title=analysis.title,
                summary=analysis.summary,
                confidence=analysis.confidence,
                extracted=analysis.extracted,
                review_flags=list(analysis.review_flags),
                extraction_error=analysis.extraction_error,
                page_count=content.page_count,
                has_text=content.has_text,
                page_texts=content.page_texts,
            )
        except Exception as error:  # noqa: BLE001
            row.update(crash=f"{type(error).__name__}: {error}", trace=traceback.format_exc()[-2000:])
        row["seconds"] = round(time.monotonic() - started, 1)
        out.write(json.dumps(row, default=str) + "\n")
        out.flush()
        print(f"[{variant}] {i}/{len(names)} {name} {row['seconds']}s", flush=True)

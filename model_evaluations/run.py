"""Run one Lexia pipeline variant over every stored document; write JSONL, never touch the DB.

usage: python run.py <variant: old|new> <model> <out.jsonl> [--user u_owner] [--names names.txt]
The variant's code must be on sys.path (PYTHONPATH) before this runs: the current
mcp/app to compare models, or two copies of it to compare code. Both variants run the
pipeline as the app does (DocumentAnalyzer with the app's settings); only the code on
the path and the model differ. --names keeps a run to the documents of an earlier one.
"""

import argparse
import json
import os
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

parser = argparse.ArgumentParser()
parser.add_argument("variant", choices=["old", "new"])
parser.add_argument("model")
parser.add_argument("out", type=Path)
parser.add_argument("--user", default="u_owner", help="whose documents: the schema and folder name")
parser.add_argument("--names", type=Path, help="a file of document names, one per line, to keep to")
options = parser.parse_args()
variant, model, out_path = options.variant, options.model, options.out
config = AppConfig.from_env()
reader = DocumentReader()
chat = OllamaChat(config.ollama_url, model, config.context_tokens)
analyzer = DocumentAnalyzer(
    chat, reader, config.max_prompt_chars, config.max_vision_pages, config.page_images_with_text
)
pipeline = DocumentPipeline(
    DoclingParser(reader, config.max_stored_text_chars, config.docling_device),
    analyzer,
    MetadataRules(config.max_tags, config.search_stop_words),
)

with psycopg.connect(config.database_url) as conn:
    stored = dict(conn.execute(f"SELECT name, doc_type FROM {options.user}.document_metadata").fetchall())

done = set()
if out_path.exists():
    done = {json.loads(line)["name"] for line in out_path.read_text().splitlines() if line.strip()}

doc_dir = Path(os.environ["DOCUMENT_DIR"]) / options.user
wanted = set(options.names.read_text().split()) if options.names else set(stored)
names = sorted(n for n in stored if n in wanted and (doc_dir / n).exists())
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

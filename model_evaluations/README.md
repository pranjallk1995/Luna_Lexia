# Model evaluations

Compare two document models on every stored document before changing
`DOCUMENT_MODEL`. Results hold personal data (payslips, IDs, tax forms) and are
kept out of git in `results/` (see `.gitignore`).

## Last evaluation

`results/2026-10-28_qwen2.5vl-vs-qwen3-vl/`: `qwen2.5vl:7b` (text only) against
`qwen3-vl:8b-instruct` (page images beside the text, identifier retries), on 89
documents. `report.md` has the verdict and the best and worst examples;
`scores.json` has per-document scores. The new model was adopted for Lexia,
Visia, and Fiora. Known regressions to fix before the next model change:

- `employee_provident_fund` and `pay_period` often null on payslips.
- Rent agreements: landlord and tenant swapped, `agreement_to` equal to `agreement_from`.
- Student and voter ID numbers left null.

## Running one

`run.py` runs one pipeline variant inside the MCP container over every stored
document and writes JSONL without touching the database; `score.py` compares
two runs:

```sh
docker exec luna_lexia-mcp-1 mkdir -p /tmp/lexia_eval
# copy each variant's mcp/app (e.g. from `git archive`) to /tmp/lexia_eval/<variant>/mcp,
# and run.py and score.py to /tmp/lexia_eval
docker exec luna_lexia-mcp-1 sh -c 'cd /tmp/lexia_eval && PYTHONPATH=old/mcp python run.py old <model> old.jsonl'
docker exec luna_lexia-mcp-1 sh -c 'cd /tmp/lexia_eval && PYTHONPATH=new/mcp python run.py new <model> new.jsonl'
docker exec luna_lexia-mcp-1 python /tmp/lexia_eval/score.py /tmp/lexia_eval /tmp/lexia_eval/new/mcp
```

`run.py` passes the page-images flag only to the `new` variant; adjust its
`DocumentAnalyzer` arguments to the code being compared. In `scores.json`,
`not_in_text` lists extracted values not found in the parsed text: wrong values,
or values read from page images when the text is thin (check those against the page).

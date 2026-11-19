# Model evaluations

Compare two document models on every stored document before changing
`DOCUMENT_MODEL`. Results hold personal data (payslips, IDs, tax forms) and are
kept out of git in `results/` (see `.gitignore`).

## Last evaluation

`results/2026-11-19_qwen3-vl-vs-qwen3-vl-abliterated/`: `qwen3-vl:8b-instruct` against
`huihui_ai/qwen3-vl-abliterated:8b-instruct`, the model Luna Socia already used for photos,
same code, on the same 89 documents. **Adopted** for Lexia, Visia, Vibia and Fiora, so one
vision model serves every app. Review flags went from 21 to 17 and fields filled from 89.2 %
to 90.0 %, with about the same share of values found in the text (96.5 %). It was 20 % faster
(20.5 instead of 25.7 minutes), and no document changed type. It reads Form 16 professional
tax (0 before, on 3 of 5), tells a voter ID from a PAN card, and leaves a rent agreement's
end date empty rather than copying the start date. Every field `qwen3.5:9b` emptied comes out
the same as before. Still open: `employee_provident_fund` and `pay_period` are often null
on payslips, an offer letter's variable pay is wrong, and payslip `currency` switches
between "INR" and "RS.".

`results/2026-11-10_qwen3-vl-vs-qwen3.5-9b/`: `qwen3-vl:8b-instruct` against
`qwen3.5:9b`, same code, on the 89 documents of the evaluation before it. **Not adopted**:
the 9B fixes two of the regressions below (PF amounts on payslips, ID numbers) and
gets more rent-agreement landlords right, but leaves fields empty that the 8B fills:
Form 16 employer PAN (4 of 5), net and gross pay on 2 payslips, bank account digits on 11,
and rent agreements' dates, lock-in and registration. It is about 25 % faster.
`report.md` has the tables.

`results/2026-10-28_qwen2.5vl-vs-qwen3-vl/`: `qwen2.5vl:7b` (text only) against
`qwen3-vl:8b-instruct` (page images beside the text, identifier retries), on 89
documents. The new model was adopted for Lexia, Visia, and Fiora. Known regressions,
still open with `qwen3-vl:8b-instruct`:

- `employee_provident_fund` and `pay_period` often null on payslips.
- Rent agreements: landlord and tenant swapped, `agreement_to` equal to `agreement_from`.
- Student and voter ID numbers left null.

## Running one

`run.py` runs the document pipeline inside the MCP container over every stored
document and writes JSONL without touching the database; `score.py` compares two
runs. To compare models, both runs use the container's own code:

```sh
C=luna_lexia-mcp-1
docker exec $C mkdir -p /tmp/lexia_eval
docker cp model_evaluations/run.py $C:/tmp/lexia_eval/
docker cp model_evaluations/score.py $C:/tmp/lexia_eval/
L="docker exec -e PYTHONPATH=/workspace/mcp -w /tmp/lexia_eval $C python"
$L run.py old huihui_ai/qwen3-vl-abliterated:8b-instruct old.jsonl
$L run.py new <model> new.jsonl
$L score.py /tmp/lexia_eval /workspace/mcp
docker cp $C:/tmp/lexia_eval model_evaluations/results/<date>_<old>-vs-<new>
```

To compare code instead, copy each version's `mcp/app` (for example from
`git archive`) to `/tmp/lexia_eval/<variant>/mcp` and point `PYTHONPATH` at it.
`--names names.txt` keeps a run to the documents of an earlier one (one name per
line), so results stay comparable as the vault grows; `--user` picks another
person's vault (default `u_owner`). A run that stops resumes where it left off;
delete a row with `crash` from the JSONL to retry that document. In `scores.json`,
`not_in_text` lists extracted values not found in the parsed text: wrong values,
or values read from page images when the text is thin (check those against the page).

Run Luna Sentia's `model_checks/check_model.py` on the new model first: it checks
vision, JSON output, `think: false` and the GPU fit in a minute.

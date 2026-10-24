# Luna Lexia

![Docker required](https://img.shields.io/badge/Docker-required-2496ED?logo=docker&logoColor=white)
![Docker Compose v2](https://img.shields.io/badge/Docker_Compose-v2-2496ED?logo=docker&logoColor=white)
![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![Luna Sentia required](https://img.shields.io/badge/Luna_Sentia-required-111111?logo=ollama&logoColor=white)
![PostgreSQL included](https://img.shields.io/badge/PostgreSQL-included-4169E1?logo=postgresql&logoColor=white)
![Status: usable](https://img.shields.io/badge/Status-usable-2EA043)

## Store, read, and search your financial documents with local AI

> **Work in progress:** Luna Lexia is a local-first and desktop-first document vault under active development.

Luna Lexia is the document counterpart of [Luna Visia](https://github.com/pranjallk1995/Luna_Visia):
a private vault for payslips, Form 16s, bank statements, tax proofs, and other
personal finance documents. Every upload is read with Docling (text, OCR,
tables), classified by a local model served by [Luna Sentia](https://github.com/pranjallk1995/Luna_Sentia),
and its type-specific details are extracted into validated Pydantic schemas.
Business rules then check the details, such as net pay against gross pay minus
deductions, and flag anything that needs a person to review it.

The extracted details are what [Luna Fiora](https://github.com/pranjallk1995/Luna_Fiora)
needs to prepare tax-filing supporting documents and track finances, in the
same way Luna Vita uses Luna Visia. MCP tools let any assistant list, search,
read, download, upload, rename, correct, and delete documents.

> **Requires Luna Sentia:** Ollama and its models run in Luna Sentia. Start Sentia first: it serves the model and creates the shared `luna_sentia_network` that Lexia joins. Lexia uses `qwen2.5vl:7b` by default, the same model as Visia, so nothing extra is downloaded.

## How a document is processed

```text
Upload (UI, MCP tool, upload link, or HTTP API)
  -> unlock     a password-protected PDF is opened with its password and stored unlocked
  -> Docling    text layer, OCR for scanned pages, tables as Markdown, one entry per page
  -> classify   Pydantic AI agent: doc_type, title, summary, tags, confidence
  -> extract    Pydantic AI agent with the doc_type's schema as constrained JSON output
  -> validate   Pydantic schema checks (retried with the errors fed back to the model),
                then business rules that produce review flags
  -> store      file in the document volume, record and page text in PostgreSQL
```

- **Docling** turns PDFs and photos into Markdown page by page, so scans become
  text (RapidOCR) and every page stays citable. If Docling fails, the PDF's own
  text layer is used so an upload is never lost.
- **Pydantic AI** runs two agents on Luna Sentia's model. A schema violation is
  sent back to the model with the validation errors for another attempt.
  Lexia talks to Ollama's native `/api/chat` endpoint through a small adapter
  (`mcp/app/integrations/ollama.py`): Ollama's OpenAI-compatible endpoint
  ignores per-request options and would cap every request at a 4,096-token
  context, too small for a multi-page Form 16.
- **Review flags** come from business rules, not model retries, because asking a
  small model to make printed numbers agree invites it to invent them. Flagged
  documents carry the `needs review` tag until the details are corrected or
  marked reviewed.

Fields are nullable: a value that is not printed stays `null` and is never
guessed. Validators accept the formats Indian documents print (`1,23,456.00`,
`31/03/2025`, `FY 2024-2025`, masked PANs) and normalize them. Dates are copied
from the document as printed and parsed by the schema, which proved far more
reliable than asking a 7B model to reformat them.

## Document types

| `doc_type` | Examples | Key details extracted | Business rules |
| --- | --- | --- | --- |
| `payslip` | Monthly salary slip | Employer, employee, PAN, UAN, pay period, earnings and deduction lines, gross, deductions, TDS, PF, net pay | Gross − deductions = net pay; lines add up to totals; valid PAN |
| `form_16` | Form 16 Part A and B | Employer TAN/PAN, employee PAN, AY/FY, regime, quarterly TDS, salary under 17(1)–(3), section 10 exemptions, standard deduction, Chapter VI-A deductions, taxable income, tax, cess, net tax payable | Quarterly TDS = total; salary head and tax computations add up; AY follows FY; employee PAN present |
| `tax_statement` | Form 26AS, AIS, TIS, yearly tax computation sheets | TDS entries by deductor and section, TCS, advance and self-assessment tax, reported salary, interest, dividends; computed income and tax | TDS entries add up to the total |
| `itr_acknowledgement` | ITR-V, return summary | ITR form, AY, acknowledgement number, filing date and section, income, tax paid, refund or balance | Not both a refund and tax due |
| `bank_statement` | Savings or current account statement | Bank, holder, account (last 4), IFSC, period, opening and closing balance, totals, interest, up to 100 transactions | Opening + credits − debits = closing; running balance per transaction; dates within the period |
| `credit_card_statement` | Card bill | Issuer, card (last 4), statement and due dates, total and minimum due, credit limit, up to 100 transactions | Minimum due ≤ total due; due date after statement date |
| `invoice` | Invoices, receipts, utility and telecom bills | Vendor and GSTIN, invoice number and date, category, line items, CGST/SGST/IGST, total, payment status | Subtotal − discount + tax = total; GST parts = total tax; lines = subtotal |
| `rent_receipt` | HRA rent receipts | Landlord and PAN, tenant, address, period, monthly rent, amount | Landlord PAN required above ₹1,00,000 annual rent |
| `investment_proof` | 80C/80D/80G proofs: insurance, ELSS, PPF, NPS, tuition, donations | Kind, section, provider, policy or account, payment date, amount, eligible deduction | — |
| `loan_statement` | Loan agreements, sanction letters, repayment schedules, interest certificates | Lender, borrowers, loan type, agreement date, amount, rate, tenure, EMI and its first and last dates, principal and interest paid, outstanding | EMIs × tenure cover the loan amount |
| `capital_gains_statement` | Broker, AMC, CAMS, or KFintech gains statement | FY, short- and long-term gains, entries by security | Short + long term = total gain |
| `identity_document` | PAN, Aadhaar, passport, licence, voter ID | Holder, number, date of birth, issue and expiry dates | Not expired |
| `offer_letter` | Job offers | Employer, designation, location, offer and joining dates, annual CTC, fixed and variable pay, bonuses, stock grant, CTC components, probation, notice period | Components add up to the CTC; joining after the offer |
| `employment_letter` | Appointment, experience, relieving, service certificate, resignation, promotion, increment letters | Letter kind, employer, employee and ID, designation, employment dates, reason, revised CTC, signatory | — |
| `resume` | Resumes and CVs | Name, contact details, headline, experience years, roles (employer, title, months), education, skills, certifications, links | — |
| `educational_certificate` | Degrees, marksheets, transcripts, SSC/HSC certificates, leaving certificates, hall tickets, score cards | Kind, holder, institution, board or university, program, semester, seat number, result, CGPA, percentage, issue date | Percentage within 0–100; CGPA within its scale; a degree has no semester |
| `recognition` | Awards, appreciation, recommendations, course certificates | Kind, title, recipient, organization, author, date, period | — |
| `rental_agreement` | Lease or leave-and-licence agreements | Landlord and PAN, tenants, address, term, lock-in, rent, maintenance, deposit, escalation, registration | Landlord PAN required above ₹1,00,000 annual rent |
| `insurance_policy` | Health, life, motor, home, travel policy schedules | Kind, insurer, policy number, policyholder, insured, nominee, period, sum insured, IDV, vehicle, premium and GST | Premium + GST = total; policy not lapsed; a policy naming a vehicle is a motor policy |
| `vehicle_registration` | Vehicle RC | Registration number, owner, class, maker, model, fuel, chassis and engine numbers, validity, RTO, financier | Registration not expired |
| `bank_account_proof` | Cancelled cheques, cheque books, passbook front pages, bank letters | Bank, holder, account (last 4), type, IFSC, MICR, branch | Valid IFSC |
| `provident_fund_document` | UAN card, PF Form 11 and Form 2, EPS and gratuity forms, PF passbook | Kind, member, UAN, PF account, establishment, joining date, nominees and shares, balances | Nominee shares total 100%; passbooks show a balance |
| `legal_document` | Legal notices, NDAs, contracts, affidavits, powers of attorney, court orders | Kind, parties, advocate, date, reference, subject, demands, deadline, amount claimed, effective dates | — |
| `medical_record` | Medical and fitness reports, prescriptions, lab reports, discharge summaries, bills | Kind, patient, provider, doctor, date, purpose, findings, fitness, amount paid | — |
| `other` | Anything else, such as onboarding and verification forms and cover letters | Issuer, recipient, date, reference, key amounts and dates | — |

Every type also gets generic checks: `*_from` dates before `*_to` dates, PAN,
TAN, GSTIN, and IFSC formats, and the type's key amount being present (for
types where every such document has one). Identity, education, career, legal,
medical, vehicle, PF, and bank-proof documents are not given a financial year
from their dates, so a 2017 marksheet is not tagged `fy 2017-18`. The
full JSON schema of each type is available from `list_document_types` and
`GET /api/document-types`. Schemas live in `mcp/app/core/schemas.py`; bump
`SCHEMA_VERSION` after changing one so `backfill_documents` re-extracts older
records.

Each record also stores indexed key facts: `financial_year` (the Indian
April–March year, from the document's own FY, its AY minus one, or its date),
`document_date`, `issuer`, and `primary_amount` (net pay, total TDS, closing
balance, invoice total, and so on). Every document is tagged with its type and
`fy 2024-25`-style year.

## Luna Fiora integration

Fiora reads Lexia over the shared Docker network with the private
`LUNALEXIA_API_TOKEN`, the same way Vita reads Visia's catalog. Unlike Visia's
image metadata, document metadata holds PANs and salaries, so **every
metadata route requires the token**:

```sh
curl -H "Authorization: Bearer $LUNALEXIA_API_TOKEN" \
  "http://luna-lexia-mcp:8000/api/metadata?doc_type=payslip&doc_type=form_16&financial_year=2024-25"
```

| Route | Purpose |
| --- | --- |
| `GET /api/metadata?doc_type=…&financial_year=…` | Visible documents with their `extracted` details and `review_flags` (repeat `doc_type` for several) |
| `GET /api/document-types` | Every type with its field schema |
| `GET /api/documents/{name}` | One document's full record and file properties |
| `GET /api/documents/{name}/text?first_page=&last_page=` | Parsed Markdown per page |
| `GET /api/public/documents/{document_id}` | The original file, by immutable ID (open, unguessable link) |

Treat documents with `review_flags` as unconfirmed, and read `null` fields as
"not printed", not zero. Store the immutable `document_id` (or `public_url`)
rather than the filename when linking a Fiora record to its supporting document.

## Start

Start Luna Sentia first, or start everything with `../luna.sh start` (Lexia is
part of the default app list):

```sh
cd ../Luna_Sentia && docker compose up -d
```

Create a private `.env` from `.env.example`:

```dotenv
POSTGRES_PASSWORD=replace-with-a-private-database-password
DOCUMENT_MODEL=qwen2.5vl:7b
DOCLING_DEVICE=auto
LUNALEXIA_PUBLIC_BASE_URL=
LUNALEXIA_API_TOKEN=replace-with-a-random-value-of-at-least-32-characters
LUNALEXIA_ALLOWED_EMAILS=you@example.com
MCP_PUBLIC_URL=http://localhost:18004
```

`LUNALEXIA_API_TOKEN` is a shared secret between the UI, the MCP server, and
peer apps such as Fiora. Generate it with `openssl rand -hex 32`.
`LUNALEXIA_PUBLIC_BASE_URL` is optional: without it documents have no
`public_url`, and ZIP download and upload links are unavailable; set it (here or
with the `set_public_base_url` tool) to the public HTTPS origin of the MCP server
to enable them.

### Google OAuth settings

1. Use a Google OAuth 2.0 **Web application** client (the one Visia uses works).
2. Add `http://localhost:18505/oauth2callback` (and your hosted callback, if any)
   as an authorized redirect URI.
3. Copy `ui/.streamlit/secrets.example.toml` to `ui/.streamlit/secrets.toml` and
   set `redirect_uri`, `cookie_secret`, `client_id`, and `client_secret`.

Create `secrets.toml` before starting the UI: Docker would otherwise create a
directory in its place.

Start Luna Lexia:

```sh
docker compose up --build
```

The first build downloads PyTorch for CUDA 13.0 and Docling's layout, table, and
OCR models into the image (about 11.6 GB), so uploads never wait on downloads and
parsing works without internet access.

- UI: <http://127.0.0.1:18505>
- Service health: <http://127.0.0.1:18004/health>
- MCP endpoint: <http://127.0.0.1:18004/mcp>

| Setting | Default |
| --- | --- |
| Database | `lunalexia` |
| Username | `lunalexia` |
| Password | `lunalexia-dev` |
| Database host inside Docker | `luna-lexia-db` |

Analysis takes about 10–40 seconds per document, almost all of it the two model
calls to Sentia; Docling parses a page in well under a second on the GPU. Inspect
the database with
[Adminer in Luna Sentia](https://github.com/pranjallk1995/Luna_Sentia#database-ui)
using server `luna-lexia-db`.

### GPU parsing

The MCP service is given the GPU (`gpus: all`), and Docling runs its layout,
table, and OCR models there. `DOCLING_DEVICE` selects the device: `auto` (the
default) uses a CUDA GPU when one is visible and the CPU otherwise, while `cuda`
or `cpu` force one. On an RTX 5070, a scanned page takes about 0.4 s instead of
2 s on the CPU and a text PDF about 15 times less, using under 1 GB of GPU
memory beside Ollama's model. On a machine without an NVIDIA GPU, remove
`gpus: all` from the `mcp` service; parsing then runs on the CPU.

### Model and context

`DOCUMENT_MODEL` must be listed in Sentia's `SENTIA_MODELS`. Lexia asks Ollama
for a 16,384-token context per request (`context_tokens` in
`mcp/app/core/config.py`) and sends at most 24,000 characters of document text.
Visia requests 8,192 tokens, so Ollama reloads the model when the two apps
alternate; that costs a few seconds and needs no configuration.

## MCP tools

### Browse and read

- `list_documents` - visible documents newest first, filterable by `doc_types` and `financial_year`, with `include_preview` for a sheet of first pages.
- `search_documents` - full-text search over filenames, titles, issuers, summaries, tags, and the parsed page text (including OCR), with tag, type, and year filters.
- `get_document` - one document's complete record: extracted details, key facts, review flags, confidence, and file properties.
- `list_document_data` - extracted details for many documents at once (up to 100), for tax filing and financial tracking.
- `read_document_text` - parsed Markdown of a page range, for details the schema does not cover.
- `list_document_types` - every type and its JSON schema.
- `get_vault_stats` - counts by type and financial year, documents needing review, failed extractions, sizes, formats, and top tags.
- `view_document` - one page rendered as an image (`page` is 1-based).
- `fetch_documents` - a numbered sheet of first pages for up to 20 documents, or `delivery="zip"` for a 24-hour download link to up to 200 originals.
- `get_document_url` - the public URL of one document.

### Add and change

- `upload_document` - add a PDF, PNG, JPEG, or WebP from a chat attachment (`file`), `document_url`, or `content_base64`, with an optional `password` for protected PDFs and an optional `doc_type`. Names are corrected and deduplicated like Visia's.
- `add_document` - add a small document from Base64.
- `create_upload_link` - single-use HTTPS link for agents with a shell; accepts a `password` form field or query parameter.
- `replace_document` - replace a document's file and re-analyze it.
- `reextract_document` - classify and extract again, optionally forcing a `doc_type`.
- `update_document_metadata` / `update_documents_metadata` - edit titles, summaries, and tags; correct `extracted` fields (validated against the type's schema, with key facts and review flags recomputed); `mark_reviewed=true` clears review flags once a person confirms the details.
- `rename_document` / `rename_documents` - rename while keeping the immutable ID and public URL.
- `set_favorite_documents`, `remove_document`, `backfill_documents`, `set_public_base_url`.

Hidden documents are never reachable through MCP tools.

## Web UI

The Streamlit UI mirrors Visia: grid and list galleries with search,
favorites, OAuth-gated hidden documents, downloads, renames, and deletion.
Cards show the top of the first page, the document type, financial year, page
count, and a `needs review` badge. The document dialog shows the pages beside
the editable title, summary, and tags; review flags with a **Mark reviewed**
button; an editable table of extracted fields; and re-extraction with a chosen
type. The upload dialog accepts a PDF password and an optional document type.

## Project structure

Lexia follows the shared Luna layout (see the Visia README):

- `mcp/app/core/` - configuration, `schemas.py` (document types, extraction schemas, business rules), and domain models.
- `mcp/app/data/` - PostgreSQL schema (with a trigger-maintained search vector), repositories, and document storage.
- `mcp/app/integrations/` - `parsing.py` (Docling), `ollama.py` (native Ollama model for Pydantic AI), `extraction.py` (classification and extraction agents), and `remote.py` (SSRF-guarded document sources).
- `mcp/app/services/` - use cases (`application.py`), PDF handling and previews (`documents.py`), ZIP downloads, and upload links.
- `mcp/app/transport/` - MCP tools, HTTP routes, bearer authentication, and branding.
- `ui/` - the Streamlit interface; `services/documents.py` renders pages with PDFium.

Run the backend tests in the built image:

```sh
docker compose build mcp
docker compose run --rm --no-deps mcp sh -c "cd tests && python -m unittest"
```

The tests use in-memory repositories, a parser that reads the PDF text layer,
and scripted model replies, so they need neither PostgreSQL, Docling's models,
nor Ollama.

## Development workflow

```sh
docker compose up --build --watch
```

UI changes sync into the running container; MCP changes sync and restart the
MCP service; requirement or Dockerfile changes rebuild.

The icons in `mcp/app/assets/` and `ui/assets/lunalexia_logo.png` are
placeholders drawn to match the Luna style; replace them with final artwork at
the same file names and sizes.

# Pennylane MCP Server

[![tests](https://github.com/lorismaz/pennylane-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/lorismaz/pennylane-mcp/actions/workflows/tests.yml)
![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![API](https://img.shields.io/badge/Pennylane-Company%20API%20v2%20%2B%20Firm%20API%20v1-brightgreen.svg)

🇫🇷 [Version française](README.md)

A [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server for **[Pennylane](https://www.pennylane.com)**, the all-in-one accounting & finance platform for French SMEs. It exposes Pennylane's **Company API v2** — and the **Firm API v1** for accounting practices — to MCP clients such as Claude Desktop, so an assistant can read your accounting data — invoices, customers, suppliers, bank transactions, the ledger and reports — and write across nearly the entire API.

- **107 tools** — the full read surface plus near-complete write coverage (create / update / delete across sales, purchases, banking, accounting, billing and mandates).
- **Multi-company** — configure any number of Pennylane companies, each with its own API token, and pick one per call.
- **Firm mode** — one accounting-firm (cabinet) token gives the 24 `pennylane_firm_*` tools access to **all the firm's client companies** (list dossiers, ledger, trial balance, FEC/AGL exports, journal entries, DMS…), selecting the client per call with `company_id`.
- **Generic read-only escape hatches** (`pennylane_get`, `pennylane_firm_get`) reach any `GET` endpoint not yet wrapped by a dedicated tool.
- **Production-friendly** — honors Pennylane's 25 req / 5 s rate limit (auto-retry on `429` via `retry-after`), cursor pagination, and the documented filter query language.

> **Two distinct APIs, two token types.** A *Company* token targets one business on `/api/external/v2`. A *Firm* token targets an accounting practice's whole client portfolio on `/api/external/firm/v1` (different endpoints and scope names — a firm token is **not** valid on v2, and vice versa). This server supports both, side by side.

## Status

**Firm API support is new and looking for real-world testers.** The 24 `pennylane_firm_*` tools were built against Pennylane's official Firm API reference and are locked in by offline contract tests, but they have not yet been exercised against the live API with a real firm token (the author doesn't have one). If you run an accounting practice and can try them, please [open an issue](https://github.com/lorismaz/pennylane-mcp/issues) with what worked and what didn't.

This is a starter server, distributed as a single `server.py`. Reads are low-risk. Writes now cover nearly the whole v2 API, including a few **destructive** actions (finalize, delete, unmatch, cancel) that are flagged with `destructiveHint` in each tool's MCP annotations. Validate write flows against a Pennylane **sandbox** token before relying on them in production. A handful of niche/BETA endpoints (e-invoice import, some mandate and bank-account fields) accept a passthrough `body`/`fields` object because Pennylane hasn't published their full schema — the API validates on submit.

## Tools

### Read tools

| Tool | Domain | Purpose |
|------|--------|---------|
| `pennylane_list_companies` | Config | List the companies configured in this server (names only) |
| `pennylane_whoami` | Config | Verify a token and show the account (`GET /me`) |
| `pennylane_list_customer_invoices` | Sales | Sales invoices & credit notes, with filters |
| `pennylane_get_customer_invoice` | Sales | One customer invoice by ID |
| `pennylane_list_customers` | Sales | Customers (company + individual) |
| `pennylane_list_products` | Sales | Products / services |
| `pennylane_list_supplier_invoices` | Purchases | Purchase invoices, with filters |
| `pennylane_get_supplier_invoice` | Purchases | One supplier invoice by ID |
| `pennylane_list_suppliers` | Purchases | Suppliers |
| `pennylane_list_transactions` | Banking | Bank transactions |
| `pennylane_list_ledger_entries` | Ledger | Journal / ledger entries |
| `pennylane_list_ledger_accounts` | Ledger | Chart of accounts (resolve account IDs) |
| `pennylane_list_journals` | Ledger | Accounting journals |
| `pennylane_get_trial_balance` | Reports | Trial balance (*balance générale*) for a period |
| `pennylane_get` | Generic | **Any v2 `GET` endpoint** (quotes, journals, categories, changelogs, payments, …) |

### Write tools

Near-complete v2 write coverage — **68 write tools** across every domain:

| Domain | What you can write |
|--------|--------------------|
| **Customer invoices** | create / update / delete draft · finalize · send by email · mark paid · import (PDF) · create from quote · categorize · link credit note · e-invoicing (import, send to PA) · upload appendix |
| **Quotes** | create · update · set status · send by email · upload appendix |
| **Customers & products** | create / update company & individual customers · create / update products · categorize |
| **Suppliers & supplier invoices** | create / update supplier · import (PDF) · update · payment & e-invoice status · validate accounting · categorize · link purchase request |
| **Banking & reconciliation** | create / update transaction · match & **unmatch** transactions · categorize · create bank account |
| **Accounting** | create journal · create / update ledger account · create / update ledger entry · letter / unletter lines · create / update categories · trigger FEC / GL / analytical exports |
| **Billing & files** | create / update billing subscription · upload file attachment · upload appendices (invoice / quote / document) |
| **Direct-debit mandates** | SEPA create / update / delete · GoCardless associate / email / cancel · Pro Account migrate / email |

⚠️ **Handle with care** — these change or remove legal/accounting state and are marked `destructiveHint` in their MCP annotations:

- **Irreversible:** `finalize_customer_invoice`, `create_customer_invoice_from_quote` (with `draft=false`)
- **Delete / undo:** `delete_draft_customer_invoice`, `unmatch_*_transaction`, `unletter_ledger_entry_lines`, `delete_sepa_mandate`, `cancel_gocardless_mandate`
- **Sends real mail:** `send_customer_invoice_by_email`, `send_quote_by_email`, `send_customer_invoice_to_pa`, `*_mail_request`

Everything else is create/update. **Monetary amounts are strings** across the whole API.

**Expense-import flow:** `pennylane_upload_file_attachment` (returns an `id`) → `pennylane_import_supplier_invoice` (pass it as `file_attachment_id`). Amounts are strings, and invoice-line totals must sum to the invoice total.

### Firm tools (accounting practices)

Enabled by `PENNYLANE_FIRM_API_KEY`. Every company-scoped tool takes a `company_id` (the numeric Pennylane ID of the client company / dossier) — start with `pennylane_firm_list_companies` to discover them, or set `PENNYLANE_FIRM_DEFAULT_COMPANY_ID`.

| Tool | Purpose |
|------|---------|
| `pennylane_firm_list_companies` | List the firm's client companies (**page/per_page** pagination) |
| `pennylane_firm_get_company` | One client company by ID |
| `pennylane_firm_list_customers` / `_suppliers` | A client's customers / suppliers |
| `pennylane_firm_list_journals` / `_ledger_accounts` / `_ledger_entries` / `_ledger_entry_lines` / `_fiscal_years` | A client's accounting structure & book |
| `pennylane_firm_get_trial_balance` | A client's trial balance for a period (**page/per_page**) |
| `pennylane_firm_get` | **Any company-scoped firm `GET` endpoint** (categories, DMS, changelogs, export polling, bank accounts, transactions, …) |
| `pennylane_firm_create_journal` / `_create_ledger_account` / `_update_ledger_account` | Chart-of-accounts setup |
| `pennylane_firm_create_ledger_entry` / `_update_ledger_entry` | Balanced journal entries (string amounts, debits = credits) |
| `pennylane_firm_create_fiscal_year` | Consecutive, non-overlapping fiscal years |
| `pennylane_firm_create_transaction` / `_update_transaction` / `_create_bank_account` | Banking |
| `pennylane_firm_create_export` | FEC / analytical general ledger export (async — poll with `pennylane_firm_get`) |
| `pennylane_firm_upload_file_attachment` / `_upload_dms_file` / `_create_dms_folder` | Files & DMS (GED) |

The Firm API surface is accounting-focused: it has **no** customer-invoice/quote/product creation — those remain Company-API-only.

## Requirements

- Python **3.10+**
- A Pennylane **Company API** token (one per company), created in Pennylane under **Settings → Connectivity / API** — and/or a **Firm API** token (accounting practices), created under **Firm settings → Firm Tokens**.

## Installation

```bash
git clone https://github.com/lorismaz/pennylane-mcp.git
cd pennylane-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

The token's **scopes** determine what the server can do. For the read tools, grant the `:readonly` scopes (e.g. `customer_invoices:readonly`, `suppliers:readonly`, `transactions:readonly`, `ledger_accounts:readonly`, `ledger_entries:readonly`, `trial_balance:readonly`). For the write tools, grant `customers:all`, `customer_invoices:all`, `supplier_invoices:all` and `file_attachments:all` as needed.

> **Tip:** create a **sandbox** first (profile menu → *Test environment*) and use its token while developing.

Copy the example env file and add your tokens:

```bash
cp .env.example .env
# edit .env and paste your real token(s)
```

`server.py` **auto-loads `.env`** from its own folder or the current directory — you do **not** need to `source` it. Existing environment variables always take precedence, so a Claude Desktop `env` block is never overridden.

### Multi-company

Company names are entirely up to you — they come from your env config and are never hardcoded. The simplest, shell-safe setup is one variable per company:

```bash
PENNYLANE_API_KEY_ACME=<acme_token>
PENNYLANE_API_KEY_BETA=<beta_token>
PENNYLANE_DEFAULT_COMPANY=acme
```

Alternatives:

| Variable | Use |
|----------|-----|
| `PENNYLANE_API_KEY_<NAME>` | One token per company; `<NAME>` becomes the company name (recommended) |
| `PENNYLANE_COMPANIES` | A single JSON object: `'{"acme":"...","beta":"..."}'` (use single quotes if you `source` the file) |
| `PENNYLANE_API_KEY` (+ `PENNYLANE_COMPANY_NAME`) | Single-company mode |
| `PENNYLANE_DEFAULT_COMPANY` | Which company a tool call uses when `company` is omitted |
| `PENNYLANE_USE_2026_CHANGES` | Opt in/out of the 2026 API behavior (default `true`; Company v2 only) |
| `PENNYLANE_API_BASE_URL` | Override the API base (rarely needed) |

Prefer strong isolation over cross-company queries? Register `server.py` more than once in your MCP client, each instance in single-company mode with its own `PENNYLANE_API_KEY` — the client namespaces the tools per server.

### Firm mode (accounting practices)

Set the firm token (combinable with any company-token setup above):

```bash
PENNYLANE_FIRM_API_KEY=<firm_token>
# optional: the client company used when a firm tool call omits company_id
PENNYLANE_FIRM_DEFAULT_COMPANY_ID=12345
```

| Variable | Use |
|----------|-----|
| `PENNYLANE_FIRM_API_KEY` | Firm (cabinet) token — enables the `pennylane_firm_*` tools (`PENNYLANE_FIRM_TOKEN` works as an alias) |
| `PENNYLANE_FIRM_DEFAULT_COMPANY_ID` | Default client-company ID when `company_id` is omitted (optional) |
| `PENNYLANE_FIRM_API_BASE_URL` | Override the firm API base (rarely needed) |

Firm-token **scopes use the firm naming** (e.g. `companies:readonly`, `journals:all`, `ledger_accounts:all`, `ledger_entries:all`, `trial_balance:readonly`, `exports:fec`, `exports:agl`, `dms_files:all`, `file_attachments:all`, `customers:readonly`, `suppliers:readonly`, `bank_accounts:all`, `transactions:all`, `fiscal_years:all`, `categories:readonly`) — grant `companies:readonly` at minimum so `pennylane_firm_list_companies` works.

### Verify

```bash
python server.py --help    # prints config + lists your configured companies
```

Then, from an MCP client, call `pennylane_whoami` to confirm the token works.

## Claude Desktop

Add this to `claude_desktop_config.json` (**Settings → Developer → Edit Config**), using absolute paths:

```json
{
  "mcpServers": {
    "pennylane": {
      "command": "/full/path/to/pennylane-mcp/.venv/bin/python",
      "args": ["/full/path/to/pennylane-mcp/server.py"],
      "env": {
        "PENNYLANE_COMPANIES": "{\"acme\":\"<acme_token>\",\"beta\":\"<beta_token>\"}",
        "PENNYLANE_DEFAULT_COMPANY": "acme"
      }
    }
  }
}
```

Restart Claude Desktop and the Pennylane tools will appear.

## Claude Code skill

The repo ships an [Agent Skill](https://code.claude.com/docs/en/skills) at [`skills/pennylane/SKILL.md`](skills/pennylane/SKILL.md) that teaches Claude the things individual tool docstrings can't: cross-tool workflows (invoice lifecycle, PDF expense import, reconciliation), the write-safety tiers with confirmation gates, aggregation discipline (paginate fully, decimal arithmetic), and a French-accounting (PCG) cheat sheet for answering finance questions from the trial balance.

It loads automatically for Claude Code sessions inside this repo (via a symlink at `.claude/skills/pennylane`). To use it everywhere the MCP server is configured, copy it to your global skills directory:

```bash
cp -r skills/pennylane ~/.claude/skills/pennylane
```

For **Claude Desktop**, add it via Settings → Capabilities → Skills (upload the `skills/pennylane` folder or a zip of it).

## Usage notes

- **Pick a company** by passing `company: "beta"` on any tool; omit it to use the default. On firm tools, pick the client company with `company_id` (numeric) instead.
- **Filters** use Pennylane's array syntax: `[{"field":"date","operator":"gteq","value":"2026-01-01"}]`. Operators: `eq, not_eq, lt, lteq, gt, gteq, in, not_in, start_with`. Booleans take string values (`"true"` / `"false"`).
- **Pagination** is cursor-based: responses include `has_more` and `next_cursor`; pass `next_cursor` back as `cursor`. Exceptions on the firm API: `pennylane_firm_list_companies` and `pennylane_firm_get_trial_balance` paginate with `page`/`per_page`.
- **Money is strings.** Pennylane v2 expects amounts like `"100.00"`, not numbers.
- **Rate limit:** 25 requests / 5 s per token. The server auto-retries on `429` using `retry-after`.
- **2026 API changes:** the server sends `X-Use-2026-API-Changes: true` by default (this behavior is mandatory from 2026-07-01). Set `PENNYLANE_USE_2026_CHANGES=false` only if you temporarily need the legacy behavior. The firm API is unaffected — the header is never sent there.
- **Reporting at scale:** for full ledger extracts, prefer the FEC / Analytical General Ledger **export** endpoints plus the **changelog** endpoints (via `pennylane_get`) instead of repeatedly listing all ledger entries.

## Security

- Tokens are read from the environment and are **never** returned by any tool.
- `.env` is git-ignored by default (see `.gitignore`) — keep your real tokens there and never commit them. If a token is ever exposed, rotate it in Pennylane.
- Prefer `:readonly` scopes unless a workflow truly needs to write.

## Contributing

Issues and pull requests are welcome. The whole server lives in `server.py`; each tool is a decorated function with a docstring that guides the model, so adding an endpoint usually means adding one function.

### Tests

Offline contract tests assert that every tool sends the correct HTTP **method and path** (httpx is mocked, so nothing hits the real API and no token is sent). A coverage guard fails if a tool is added without a matching test case.

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

## License

[MIT](LICENSE)

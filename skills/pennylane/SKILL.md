---
name: pennylane
description: >-
  Work with Pennylane (French accounting & finance platform) through the
  pennylane_* MCP tools — read and write invoices, quotes, customers,
  suppliers, bank transactions, the ledger, and reports. Use this skill
  whenever the user mentions Pennylane, invoices/factures, quotes/devis,
  credit notes/avoirs, suppliers/fournisseurs, bank reconciliation,
  lettrage, VAT/TVA, trial balance/balance générale, FEC exports, or any
  French bookkeeping task, even if they don't name Pennylane explicitly —
  if pennylane_* tools are available, consult this skill before calling them.
---

# Using the Pennylane MCP

The server's 83 tools each document their own parameters, filters, and field
semantics in their descriptions — read those for per-tool details (they're
good). This skill covers only what individual tool descriptions can't:
cross-call discipline, write safety, multi-step workflows, and the French
accounting context needed to answer finance questions correctly.

## Cross-call discipline

- **Never aggregate a partial dataset.** For any total, count, "most/least",
  or comparison: page with `cursor`/`next_cursor` until `has_more` is `false`
  (or a filter provably bounds the data) before computing. A sum over page
  one is a wrong answer, not an approximation.
- **Compute programmatically.** Amounts are strings — parse them as decimals
  (never floats) and do the arithmetic in a script or explicit steps, not
  mentally.
- **Filter server-side where the endpoint supports it, client-side where it
  doesn't.** Each list tool's description names its server-filterable fields;
  notably `paid`/`status` on invoices are NOT among them — date-bound the
  query, then filter the results yourself.
- **Multiple companies configured?** Every tool takes `company`. If the
  request is ambiguous about which company, ask — you'd be reading (or
  writing!) the wrong books. `pennylane_list_companies` shows what's set up.
- **Errors:** tools return error text instead of raising. A 403 means the
  token lacks a scope — say so rather than retrying. Start troubleshooting
  with `pennylane_whoami`.

## Write safety — three tiers

1. **Reversible creates/updates** (drafts, customers, products, quotes,
   categories…): fine when asked. Default to drafts.
2. **Destructive / undo** (`delete_*`, `unmatch_*`, `unletter_*`,
   `cancel_*`): confirm the target ID with the user before calling.
3. **Irreversible or outward-facing:** finalizing an invoice assigns a
   **legal invoice number** — there is no un-finalize in French accounting,
   only credit notes. Sending (email, e-invoicing platform) reaches real
   recipients.

   Never chain into tier 3 implicitly — "create an invoice for Acme" means a
   draft. Use **two separate gates**: show the draft and get explicit
   approval to finalize; then confirm the recipient address immediately
   before sending. If the user is experimenting, suggest a sandbox token.

## Workflow sequencing

- **Invoice lifecycle:** find/create the customer (customers must exist
  first — v2 never creates them inline) → create draft → *gate: finalize* →
  *gate: send* → record payment by matching the real bank transaction
  (preferred) or `mark_customer_invoice_as_paid`.
- **Quote → invoice:** create → send → update status (accepted) → create
  invoice from quote, keeping `draft=true`, then the finalize gates above.
- **Supplier invoice from PDF:** two steps — upload the file attachment,
  then import using the returned `file_attachment_id`.
- **Reconciliation:** operational level = match transactions to invoices
  (`match_*_transaction`); ledger level = lettering (`letter_ledger_entry_lines`).
- **Manual journal entry:** resolve internal IDs first (`list_journals`,
  `list_ledger_accounts` — tools want IDs, not account numbers like "601"),
  then create the balanced entry.
- **Categorize means REPLACE:** the categorize tools overwrite the
  resource's category list. When the user means "add", read current
  categories first (via `pennylane_get` path `categories` for IDs).

## Reporting — pick by scale

- **Period snapshot** → `pennylane_get_trial_balance` (the *balance
  générale*). The backbone for most finance questions; see the account map
  below.
- **Full book extract** → `pennylane_create_export` (FEC / general ledger),
  async: poll with `pennylane_get` until the download URL appears. Don't
  page through all ledger entries on a large book.
- **"What changed since X"** → changelog endpoints via `pennylane_get`
  (e.g. path `customers/changes`).
- **No dedicated tool?** → `pennylane_get` reaches every v2 GET endpoint
  (quotes, categories, bank_accounts, `customer_invoices/{id}/payments`, …)
  and can never modify data.

## French accounting cheat sheet (PCG)

Account numbers tell you what a balance means:

| Class | Contents | Notes |
|---|---|---|
| 1 | Equity & loans | capital, emprunts |
| 2 | Fixed assets | immobilisations |
| 3 | Inventory | stocks |
| 4 | Third parties | 401 suppliers (payables), 411 customers (receivables), 4456/4457 VAT deductible/collected |
| 5 | Cash & bank | 512 bank accounts |
| 6 | **Expenses** | charges — P&L cost side |
| 7 | **Revenue** | produits — 70x is *chiffre d'affaires* (sales) |

So on the trial balance for a period: **revenue** = class 7 (credits minus
debits; lead with class 70 for chiffre d'affaires, VAT excluded), a rough
P&L = class 7 − class 6, receivables = 411 balances (operationally: unpaid
invoices' `remaining_amount_with_tax`). **Lettrage** = matching debits
against credits on third-party accounts. **FEC** = the legal full-ledger
export French tax authorities require.

French → English mapping: facture = invoice, devis = quote, avoir = credit
note, fournisseur = supplier, échéance = deadline/due date, rapprochement =
reconciliation.

"""Contract tests: every tool must hit the correct HTTP method and path.

These are offline tests — httpx is patched with a MockTransport that records the
outgoing request and returns a canned 200, so nothing touches the real Pennylane
API and no token is ever sent anywhere. The point is to lock in the method+path
(and a few tricky request bodies) that were verified against the v2 reference, so
a future edit can't silently regress them.

Run:  pip install -r requirements.txt -r requirements-dev.txt && pytest
"""
import asyncio
import json
import tempfile

import httpx
import pytest

import server

# A throwaway PDF on disk for the multipart upload tools.
_tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
_tmp.write(b"%PDF-1.4 test file")
_tmp.close()
TMP_PDF = _tmp.name

LINE = {"label": "Consulting", "quantity": 1, "unit": "hour",
        "raw_currency_unit_price": "100.00", "vat_rate": "FR_200"}
LEDGER_LINES = [
    {"ledger_account_id": 1, "label": "debit", "debit": "100.00", "credit": "0"},
    {"ledger_account_id": 2, "label": "credit", "debit": "0", "credit": "100.00"},
]
FIELDS = {"label": "changed"}

S = server


@pytest.fixture
def http(monkeypatch):
    """Patch httpx so every request is recorded and answered with a canned 200."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(server.httpx, "AsyncClient", factory)
    # Deterministic single-company config — never the user's real tokens.
    monkeypatch.setattr(server, "COMPANIES", {"acme": "test-token"})
    monkeypatch.setattr(server, "DEFAULT_COMPANY", None)
    return requests


# (tool coroutine, params instance, expected method, expected path under /v2)
CASES = [
    # --- reads ---
    (S.pennylane_whoami, S.CompanyOnly(), "GET", "me"),
    (S.pennylane_list_customer_invoices, S.ListInput(), "GET", "customer_invoices"),
    (S.pennylane_get_customer_invoice, S.GetByIdInput(id="1"), "GET", "customer_invoices/1"),
    (S.pennylane_list_customers, S.ListInput(), "GET", "customers"),
    (S.pennylane_list_products, S.ListInput(), "GET", "products"),
    (S.pennylane_list_supplier_invoices, S.ListInput(), "GET", "supplier_invoices"),
    (S.pennylane_get_supplier_invoice, S.GetByIdInput(id="1"), "GET", "supplier_invoices/1"),
    (S.pennylane_list_suppliers, S.ListInput(), "GET", "suppliers"),
    (S.pennylane_list_transactions, S.ListInput(), "GET", "transactions"),
    (S.pennylane_list_ledger_entries, S.ListInput(), "GET", "ledger_entries"),
    (S.pennylane_list_ledger_accounts, S.ListInput(), "GET", "ledger_accounts"),
    (S.pennylane_list_journals, S.ListInput(), "GET", "journals"),
    (S.pennylane_get_trial_balance,
     S.TrialBalanceInput(period_start="2026-01-01", period_end="2026-12-31"),
     "GET", "trial_balance"),
    (S.pennylane_get, S.GenericGetInput(path="quotes"), "GET", "quotes"),

    # --- creates / lifecycle (incl. the four fixed bugs) ---
    (S.pennylane_create_individual_customer,
     S.CreateIndividualCustomerInput(first_name="A", last_name="B"),
     "POST", "customers/individual"),
    (S.pennylane_create_company_customer,
     S.CreateCompanyCustomerInput(name="Acme"), "POST", "customers/company"),
    (S.pennylane_create_supplier, S.CreateSupplierInput(name="Vend"), "POST", "suppliers"),
    (S.pennylane_create_draft_customer_invoice,
     S.CreateDraftInvoiceInput(customer_id=1, date="2026-01-01"), "POST", "customer_invoices"),
    (S.pennylane_create_export,
     S.CreateExportInput(kind="fec", period_start="2026-01-01", period_end="2026-12-31"),
     "POST", "exports/fecs"),
    (S.pennylane_finalize_customer_invoice, S.GetByIdInput(id="1"),
     "POST", "customer_invoices/1/finalize"),
    (S.pennylane_send_customer_invoice_by_email, S.SendInvoiceEmailInput(id="1"),
     "POST", "customer_invoices/1/send_by_email"),
    (S.pennylane_mark_customer_invoice_as_paid, S.GetByIdInput(id="1"),
     "POST", "customer_invoices/1/mark_as_paid"),
    (S.pennylane_match_customer_invoice_transaction,
     S.MatchTransactionInput(invoice_id=1, transaction_id=2),
     "POST", "customer_invoices/1/matched_transactions"),
    (S.pennylane_match_supplier_invoice_transaction,
     S.MatchTransactionInput(invoice_id=1, transaction_id=2),
     "POST", "supplier_invoices/1/matched_transactions"),
    (S.pennylane_upload_file_attachment, S.UploadFileInput(file_path=TMP_PDF),
     "POST", "file_attachments"),
    (S.pennylane_import_supplier_invoice,
     S.ImportSupplierInvoiceInput(file_attachment_id=1, date="2026-01-01"),
     "POST", "supplier_invoices/import"),

    # --- CRUD completion ---
    (S.pennylane_update_customer_invoice, S.UpdateByIdInput(id="1", fields=FIELDS),
     "PATCH", "customer_invoices/1"),
    (S.pennylane_delete_draft_customer_invoice, S.GetByIdInput(id="1"),
     "DELETE", "customer_invoices/1"),
    (S.pennylane_update_company_customer, S.UpdateByIdInput(id="1", fields=FIELDS),
     "PUT", "customers/company/1"),
    (S.pennylane_update_individual_customer, S.UpdateByIdInput(id="1", fields=FIELDS),
     "PUT", "customers/individual/1"),
    (S.pennylane_update_supplier, S.UpdateByIdInput(id="1", fields=FIELDS), "PUT", "suppliers/1"),
    (S.pennylane_update_supplier_invoice, S.UpdateByIdInput(id="1", fields=FIELDS),
     "PUT", "supplier_invoices/1"),
    (S.pennylane_create_product, S.CreateProductInput(label="Svc"), "POST", "products"),
    (S.pennylane_update_product, S.UpdateByIdInput(id="1", fields=FIELDS), "PUT", "products/1"),

    # --- quotes ---
    (S.pennylane_create_quote,
     S.CreateQuoteInput(customer_id=1, date="2026-01-01", deadline="2026-02-01",
                        invoice_lines=[LINE]), "POST", "quotes"),
    (S.pennylane_update_quote, S.UpdateByIdInput(id="1", fields=FIELDS), "PATCH", "quotes/1"),
    (S.pennylane_update_quote_status, S.QuoteStatusInput(id="1", status="accepted"),
     "PUT", "quotes/1/update_status"),
    (S.pennylane_send_quote_by_email, S.SendQuoteEmailInput(id="1"),
     "POST", "quotes/1/send_by_email"),
    (S.pennylane_create_customer_invoice_from_quote, S.InvoiceFromQuoteInput(quote_id=9),
     "POST", "customer_invoices/create_from_quote"),

    # --- transactions & unmatch ---
    (S.pennylane_create_transaction,
     S.CreateTransactionInput(bank_account_id=1, label="x", date="2026-01-01", amount="1.00"),
     "POST", "transactions"),
    (S.pennylane_update_transaction, S.UpdateByIdInput(id="1", fields=FIELDS),
     "PUT", "transactions/1"),
    (S.pennylane_unmatch_customer_invoice_transaction,
     S.UnmatchTransactionInput(invoice_id=1, matched_transaction_id=2),
     "DELETE", "customer_invoices/1/matched_transactions/2"),
    (S.pennylane_unmatch_supplier_invoice_transaction,
     S.UnmatchTransactionInput(invoice_id=1, matched_transaction_id=2),
     "DELETE", "supplier_invoices/1/matched_transactions/2"),

    # --- categorization ---
    (S.pennylane_categorize_customer_invoice,
     S.CategorizeInput(id="1", categories=[{"id": 1, "weight": "1"}]),
     "PUT", "customer_invoices/1/categories"),
    (S.pennylane_categorize_supplier_invoice,
     S.CategorizeInput(id="1", categories=[{"id": 1, "weight": "1"}]),
     "PUT", "supplier_invoices/1/categories"),
    (S.pennylane_categorize_customer,
     S.CategorizeInput(id="1", categories=[{"id": 1, "weight": "1"}]),
     "PUT", "customers/1/categories"),
    (S.pennylane_categorize_supplier,
     S.CategorizeInput(id="1", categories=[{"id": 1, "weight": "1"}]),
     "PUT", "suppliers/1/categories"),
    (S.pennylane_categorize_transaction,
     S.CategorizeInput(id="1", categories=[{"id": 1, "weight": "1"}]),
     "PUT", "transactions/1/categories"),
    (S.pennylane_categorize_ledger_entry_line,
     S.CategorizeInput(id="1", categories=[{"id": 1, "weight": "1"}]),
     "PUT", "ledger_entry_lines/1/categories"),
    (S.pennylane_create_category, S.CreateCategoryInput(label="Mktg"), "POST", "categories"),
    (S.pennylane_update_category, S.UpdateByIdInput(id="1", fields=FIELDS), "PUT", "categories/1"),

    # --- accounting structure ---
    (S.pennylane_create_journal, S.CreateJournalInput(label="Sales"), "POST", "journals"),
    (S.pennylane_create_ledger_account,
     S.CreateLedgerAccountInput(number="706000", label="Rev"), "POST", "ledger_accounts"),
    (S.pennylane_update_ledger_account, S.UpdateByIdInput(id="1", fields=FIELDS),
     "PATCH", "ledger_accounts/1"),
    (S.pennylane_create_ledger_entry,
     S.CreateLedgerEntryInput(date="2026-01-01", journal_id=1, ledger_entry_lines=LEDGER_LINES),
     "POST", "ledger_entries"),
    (S.pennylane_update_ledger_entry, S.UpdateByIdInput(id="1", fields=FIELDS),
     "PUT", "ledger_entries/1"),
    (S.pennylane_letter_ledger_entry_lines, S.LetteringInput(line_ids=[1, 2]),
     "POST", "ledger_entry_lines/lettering"),
    (S.pennylane_unletter_ledger_entry_lines, S.LetteringInput(line_ids=[1]),
     "DELETE", "ledger_entry_lines/lettering"),

    # --- invoice status, e-invoicing & links ---
    (S.pennylane_update_supplier_invoice_payment_status,
     S.SupplierPaymentStatusInput(id="1", payment_status="paid"),
     "PUT", "supplier_invoices/1/payment_status"),
    (S.pennylane_update_supplier_invoice_e_invoice_status,
     S.ActionInput(id="1", body={"e_invoice_status": "x"}),
     "PUT", "supplier_invoices/1/e_invoice_status"),
    (S.pennylane_validate_supplier_invoice_accounting, S.ActionInput(id="1"),
     "POST", "supplier_invoices/1/validate_accounting"),
    (S.pennylane_import_supplier_e_invoice, S.ActionInput(id="1", body={"a": 1}),
     "POST", "supplier_invoices/1/import_e_invoice"),
    (S.pennylane_send_customer_invoice_to_pa, S.ActionInput(id="1"),
     "POST", "customer_invoices/1/send_to_pa"),
    (S.pennylane_import_customer_e_invoice, S.ActionInput(id="1", body={"a": 1}),
     "POST", "customer_invoices/1/import_e_invoice"),
    (S.pennylane_link_credit_note, S.LinkCreditNoteInput(id="1", credit_note_id=2),
     "POST", "customer_invoices/1/link_credit_note"),
    (S.pennylane_link_purchase_request_to_supplier_invoice,
     S.LinkPurchaseRequestInput(id="1", purchase_request_id=2),
     "POST", "supplier_invoices/1/linked_purchase_requests"),

    # --- imports, banking, subscriptions, appendices ---
    (S.pennylane_import_customer_invoice,
     S.ImportCustomerInvoiceInput(file_attachment_id=1, date="2026-01-01", customer_id=1),
     "POST", "customer_invoices/import"),
    (S.pennylane_import_purchase_request,
     S.ImportPurchaseRequestInput(file_attachment_id=1), "POST", "purchase_requests/import"),
    (S.pennylane_create_bank_account, S.CreateWithFieldsInput(fields={"name": "Main"}),
     "POST", "bank_accounts"),
    (S.pennylane_create_billing_subscription,
     S.CreateWithFieldsInput(fields={"customer_id": 1}), "POST", "billing_subscriptions"),
    (S.pennylane_update_billing_subscription, S.UpdateByIdInput(id="1", fields=FIELDS),
     "PUT", "billing_subscriptions/1"),
    (S.pennylane_upload_customer_invoice_appendix,
     S.UploadAppendixInput(id="1", file_path=TMP_PDF), "POST", "customer_invoices/1/appendices"),
    (S.pennylane_upload_quote_appendix,
     S.UploadAppendixInput(id="1", file_path=TMP_PDF), "POST", "quotes/1/appendices"),
    (S.pennylane_upload_commercial_document_appendix,
     S.UploadAppendixInput(id="1", file_path=TMP_PDF),
     "POST", "commercial_documents/1/appendices"),

    # --- mandates ---
    (S.pennylane_create_sepa_mandate, S.CreateWithFieldsInput(fields={"customer_id": 1}),
     "POST", "sepa_mandates"),
    (S.pennylane_update_sepa_mandate, S.UpdateByIdInput(id="1", fields=FIELDS),
     "PUT", "sepa_mandates/1"),
    (S.pennylane_delete_sepa_mandate, S.GetByIdInput(id="1"), "DELETE", "sepa_mandates/1"),
    (S.pennylane_associate_gocardless_mandate,
     S.CreateWithFieldsInput(fields={"customer_id": 1}), "POST", "gocardless_mandates/associations"),
    (S.pennylane_send_gocardless_mandate_mail_request, S.ActionInput(id="1"),
     "POST", "gocardless_mandates/1/mail_requests"),
    (S.pennylane_cancel_gocardless_mandate, S.ActionInput(id="1"),
     "POST", "gocardless_mandates/1/cancellations"),
    (S.pennylane_migrate_pro_account_mandate,
     S.CreateWithFieldsInput(fields={"customer_id": 1}), "POST", "pro_account_mandates/migrations"),
    (S.pennylane_send_pro_account_mandate_mail_request,
     S.BodyOnlyInput(body={"customer_id": 1}), "POST", "pro_account_mandates/mail_requests"),
]


@pytest.mark.parametrize("func,params,method,path", CASES,
                         ids=[f.__name__ for f, *_ in CASES])
def test_method_and_path(http, func, params, method, path):
    result = asyncio.run(func(params))
    assert len(http) == 1, f"expected exactly one request; got {len(http)}. result={result!r}"
    req = http[0]
    assert req.method == method
    assert str(req.url).split("?")[0] == f"{server.API_BASE_URL}/{path}"


def test_every_registered_tool_is_covered():
    """Guard: adding a tool without a test case fails here."""
    registered = {t.name for t in server.mcp._tool_manager.list_tools()}
    covered = {f.__name__ for f, *_ in CASES} | {"pennylane_list_companies"}
    missing = registered - covered
    assert not missing, f"tools with no test case: {sorted(missing)}"


def test_list_companies_makes_no_http_call(http):
    out = asyncio.run(server.pennylane_list_companies())
    assert http == []
    assert json.loads(out)["companies"] == ["acme"]


# --- targeted body assertions for the trickiest / previously-buggy shapes ---

def test_categorize_body_is_bare_array(http):
    asyncio.run(server.pennylane_categorize_transaction(
        server.CategorizeInput(id="3", categories=[{"id": 7, "weight": "1"}])))
    assert json.loads(http[0].content) == [{"id": 7, "weight": "1"}]


def test_lettering_body_wraps_ids(http):
    asyncio.run(server.pennylane_letter_ledger_entry_lines(
        server.LetteringInput(line_ids=[5, 6])))
    body = json.loads(http[0].content)
    assert body["ledger_entry_lines"] == [{"id": 5}, {"id": 6}]
    assert body["unbalanced_lettering_strategy"] == "none"


def test_from_quote_body(http):
    asyncio.run(server.pennylane_create_customer_invoice_from_quote(
        server.InvoiceFromQuoteInput(quote_id=9)))
    body = json.loads(http[0].content)
    assert body == {"quote_id": 9, "draft": True}


def test_payment_status_body(http):
    asyncio.run(server.pennylane_update_supplier_invoice_payment_status(
        server.SupplierPaymentStatusInput(id="1", payment_status="to_be_paid")))
    assert json.loads(http[0].content) == {"payment_status": "to_be_paid"}


def test_auth_header_uses_bearer_token(http):
    asyncio.run(server.pennylane_whoami(server.CompanyOnly()))
    assert http[0].headers["authorization"] == "Bearer test-token"


def test_unknown_company_raises_no_request(http):
    out = asyncio.run(server.pennylane_whoami(server.CompanyOnly(company="nope")))
    assert http == []
    assert "Unknown company" in out

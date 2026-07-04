#!/usr/bin/env python3
"""
Pennylane MCP Server (Company API v2 + Firm API v1).

A starter Model Context Protocol server for Pennylane — the all-in-one
accounting & finance OS for French SMEs. It wraps the Company API v2 so an
agent can read invoices, suppliers, customers, transactions, the ledger and
accounting reports, and create a few core resources (customers, draft invoices).
It also wraps the Firm API v1 so an accounting firm (cabinet) token can work
across all of the firm's client companies (dossiers).

Highlights
----------
* Multi-company: configure several Pennylane companies (e.g. Acme + Beta),
  each with its own API token. Every tool accepts an optional `company`.
* Firm mode: one PENNYLANE_FIRM_API_KEY unlocks the pennylane_firm_* tools —
  a separate API (/api/external/firm/v1) where the client company is selected
  per call via `company_id` (discover IDs with pennylane_firm_list_companies).
* Rate-limit aware: respects Pennylane's 25 requests / 5 seconds limit and the
  `retry-after` header on 429s, with automatic backoff + retry.
* Cursor pagination + the documented `filter` query language are first-class.
* Forward-compatible with the 2026 API changes (sends X-Use-2026-API-Changes
  on Company v2 calls; the firm API is unaffected and never gets the header).
* Generic read-only escape hatches (`pennylane_get`, `pennylane_firm_get`)
  cover every GET endpoint that doesn't (yet) have a dedicated tool.

Auth tokens are read from the environment and are NEVER returned by any tool.

Environment variables
----------------------
PENNYLANE_API_KEY           Single-company token (optional).
PENNYLANE_COMPANY_NAME      Friendly name for PENNYLANE_API_KEY (default: "default").
PENNYLANE_COMPANIES         JSON object mapping name -> token, e.g.
                            '{"acme": "tok_xxx", "beta": "tok_yyy"}'.
                            Merged with PENNYLANE_API_KEY if both are set.
PENNYLANE_DEFAULT_COMPANY   Which configured company to use when a tool call
                            omits `company` (defaults to the only one, if single).
PENNYLANE_API_BASE_URL      Override the API base (default production v2).
PENNYLANE_USE_2026_CHANGES  "true"/"false" — opt in/out of 2026 API behavior
                            (default "true", which is the API default during the
                            2026 sunset phase and mandatory from 2026-07-01).

PENNYLANE_FIRM_API_KEY      Accounting-firm token (Pennylane: Firm settings ->
                            Firm Tokens). Enables the pennylane_firm_* tools.
                            (PENNYLANE_FIRM_TOKEN is accepted as an alias.)
PENNYLANE_FIRM_DEFAULT_COMPANY_ID
                            Numeric client-company ID used when a firm tool
                            call omits `company_id` (optional).
PENNYLANE_FIRM_API_BASE_URL Override the firm API base (default production
                            /api/external/firm/v1).
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
import asyncio
from pathlib import Path
from enum import Enum
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field, ConfigDict
from mcp.server.fastmcp import FastMCP

# --------------------------------------------------------------------------- #
# Constants & configuration
# --------------------------------------------------------------------------- #

mcp = FastMCP("pennylane_mcp")


def _load_dotenv() -> None:
    """Load KEY=VALUE pairs from a local .env into os.environ (no overriding).

    Looks next to this script and in the current working directory. Existing
    environment variables always win, so a Claude Desktop `env` block is never
    overridden. Surrounding single/double quotes are stripped here, so a JSON
    value keeps its inner quotes intact — this sidesteps the bash `source .env`
    pitfall where the shell strips the JSON's double quotes.
    """
    candidates: list[Path] = []
    try:
        candidates.append(Path(__file__).resolve().parent / ".env")
    except NameError:
        pass
    candidates.append(Path.cwd() / ".env")

    seen: set[Path] = set()
    for path in candidates:
        try:
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = val
        except OSError:
            continue


_load_dotenv()

DEFAULT_BASE_URL = "https://app.pennylane.com/api/external/v2"
API_BASE_URL = os.environ.get("PENNYLANE_API_BASE_URL", DEFAULT_BASE_URL).rstrip("/")

# Pennylane allows 25 requests / 5s per token. We retry on 429 using the
# server-provided `retry-after` header.
MAX_RETRIES = 4
REQUEST_TIMEOUT = 30.0

# During the 2026 sunset phase (Apr 8 - Jun 30 2026) the new behavior is the
# default; from Jul 1 2026 it is the only behavior. Default to opting in.
USE_2026_CHANGES = os.environ.get("PENNYLANE_USE_2026_CHANGES", "true").lower() == "true"


class ResponseFormat(str, Enum):
    """Output format for tool responses."""
    JSON = "json"
    MARKDOWN = "markdown"


def _parse_companies_blob(raw: str) -> dict[str, str]:
    """Parse PENNYLANE_COMPANIES into {name: token}.

    Accepts strict JSON ({"acme":"tok"}). Falls back to a lenient
    'name:token,other:tok2' / 'name=token;...' form — which also recovers the
    common case where bash `source .env` stripped the JSON's double quotes,
    turning {"acme":"tok"} into {acme:tok}.
    """
    raw = raw.strip()
    if not raw:
        return {}
    # 1) Strict JSON (the happy path when the value reaches us with quotes intact).
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {
                str(k).strip().lower(): str(v)
                for k, v in parsed.items()
                if str(k).strip() and str(v)
            }
    except json.JSONDecodeError:
        pass
    # 2) Lenient pair parsing (also handles quote-stripped JSON-ish input).
    body = raw.lstrip("{").rstrip("}")
    out: dict[str, str] = {}
    for chunk in re.split(r"[;,]", body):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"""^["']?([^"':=]+)["']?\s*[:=]\s*["']?(.+?)["']?$""", chunk)
        if m:
            name, token = m.group(1).strip().lower(), m.group(2).strip()
            if name and token:
                out[name] = token
    return out


def _load_companies() -> dict[str, str]:
    """Build the {company_name: token} registry from the environment.

    Resolution order (later sources fill gaps, never override earlier ones):
      1. PENNYLANE_COMPANIES        — JSON object, or lenient name:token pairs.
      2. PENNYLANE_API_KEY_<NAME>   — one token per company, quote-free & shell-safe.
      3. PENNYLANE_API_KEY (+ PENNYLANE_COMPANY_NAME) — single company.

    Tokens are kept in this process only and never exposed by any tool.
    """
    registry: dict[str, str] = {}

    raw = os.environ.get("PENNYLANE_COMPANIES")
    if raw:
        parsed = _parse_companies_blob(raw)
        if parsed:
            registry.update(parsed)
        else:
            print(
                "[pennylane_mcp] WARNING: PENNYLANE_COMPANIES could not be parsed. "
                "Easiest fixes: (a) keep your tokens in a .env file next to "
                "server.py (it is auto-loaded, no `source` needed), or (b) use "
                "one PENNYLANE_API_KEY_<NAME> variable per company instead. If you "
                "do `source .env`, wrap the JSON in SINGLE quotes: "
                """PENNYLANE_COMPANIES='{"acme":"..."}'.""",
                file=sys.stderr,
            )

    # Per-company tokens: PENNYLANE_API_KEY_<NAME>=token (no quotes/braces needed).
    prefix = "PENNYLANE_API_KEY_"
    for env_key, token in os.environ.items():
        if env_key.startswith(prefix) and token:
            name = env_key[len(prefix):].strip().lower()
            if name:
                registry.setdefault(name, token)

    single = os.environ.get("PENNYLANE_API_KEY")
    if single:
        name = os.environ.get("PENNYLANE_COMPANY_NAME", "default").strip().lower()
        registry.setdefault(name, single)

    return registry


COMPANIES = _load_companies()
DEFAULT_COMPANY = os.environ.get("PENNYLANE_DEFAULT_COMPANY", "").strip().lower() or None

# --- Firm API (cabinet / accounting-firm token) --------------------------- #
# The Firm API is a SEPARATE API from Company v2: its own base URL
# (/api/external/firm/v1), the client company selected via
# /companies/{company_id}/ in the path, and its own scopes. A firm token is
# NOT valid on the v2 endpoints and vice versa.
DEFAULT_FIRM_BASE_URL = "https://app.pennylane.com/api/external/firm/v1"
FIRM_API_BASE_URL = os.environ.get(
    "PENNYLANE_FIRM_API_BASE_URL", DEFAULT_FIRM_BASE_URL
).rstrip("/")
FIRM_TOKEN = (
    os.environ.get("PENNYLANE_FIRM_API_KEY")
    or os.environ.get("PENNYLANE_FIRM_TOKEN")  # alias, for parity with other tools
    or None
)


def _load_firm_default_company_id() -> Optional[int]:
    raw = os.environ.get("PENNYLANE_FIRM_DEFAULT_COMPANY_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        print(
            f"[pennylane_mcp] WARNING: PENNYLANE_FIRM_DEFAULT_COMPANY_ID={raw!r} "
            "is not an integer and was ignored.",
            file=sys.stderr,
        )
        return None


FIRM_DEFAULT_COMPANY_ID = _load_firm_default_company_id()


# --------------------------------------------------------------------------- #
# Core infrastructure: auth resolution, HTTP client, errors, formatting
# --------------------------------------------------------------------------- #

class PennylaneError(Exception):
    """Raised with an agent-friendly message when a request cannot proceed."""


def _resolve_company(company: Optional[str]) -> str:
    """Pick which configured company to use, or raise an actionable error."""
    if not COMPANIES:
        raise PennylaneError(
            "No Pennylane companies are configured. Set PENNYLANE_API_KEY or "
            "PENNYLANE_COMPANIES in the environment. See the README."
        )
    if company:
        key = company.strip().lower()
        if key not in COMPANIES:
            available = ", ".join(sorted(COMPANIES)) or "(none)"
            raise PennylaneError(
                f"Unknown company '{company}'. Configured companies: {available}."
            )
        return key
    if DEFAULT_COMPANY and DEFAULT_COMPANY in COMPANIES:
        return DEFAULT_COMPANY
    if len(COMPANIES) == 1:
        return next(iter(COMPANIES))
    available = ", ".join(sorted(COMPANIES))
    raise PennylaneError(
        f"Multiple companies are configured ({available}) and no default is set. "
        "Pass `company` explicitly or set PENNYLANE_DEFAULT_COMPANY."
    )


def _headers(token: str, json_content: bool = True, use_2026: bool = True) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if json_content:
        # Omitted for multipart uploads so httpx can set the boundary itself.
        headers["Content-Type"] = "application/json"
    if USE_2026_CHANGES and use_2026:
        # The 2026 opt-in only exists on Company v2 — never sent to firm/v1.
        headers["X-Use-2026-API-Changes"] = "true"
    return headers


async def _send(
    method: str,
    url: str,
    headers: dict[str, str],
    path: str,
    auth_desc: str,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[Any] = None,  # dict for most endpoints; some take a JSON array
    files: Optional[dict[str, Any]] = None,
) -> Any:
    """Shared HTTP loop for both the Company v2 and Firm v1 APIs.

    Handles 429 rate limiting transparently (honoring `retry-after`) and raises
    PennylaneError with an actionable message on failure. Pass `files` for a
    multipart upload (e.g. file_attachments); otherwise `json_body` is sent.
    """
    is_multipart = files is not None

    attempt = 0
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        while True:
            try:
                resp = await client.request(
                    method.upper(),
                    url,
                    headers=headers,
                    params=params,
                    json=None if is_multipart else json_body,
                    files=files,
                )
            except httpx.TimeoutException as exc:
                raise PennylaneError(
                    f"Request to {path} timed out after {REQUEST_TIMEOUT}s. Try again."
                ) from exc
            except httpx.HTTPError as exc:
                raise PennylaneError(f"Network error calling {path}: {exc}") from exc

            if resp.status_code == 429 and attempt < MAX_RETRIES:
                try:
                    retry_after = float(resp.headers.get("retry-after", "2"))
                except (TypeError, ValueError):
                    # retry-after may be an HTTP-date rather than seconds; fall back.
                    retry_after = 2.0
                await asyncio.sleep(min(retry_after, 10.0))
                attempt += 1
                continue

            if resp.status_code >= 400:
                raise PennylaneError(_describe_http_error(resp, auth_desc))

            if resp.status_code == 204 or not resp.content:
                return {"status": "ok", "http_status": resp.status_code}
            try:
                return resp.json()
            except ValueError:
                return {"raw": resp.text, "http_status": resp.status_code}


async def _request(
    method: str,
    path: str,
    company: Optional[str],
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[Any] = None,
    files: Optional[dict[str, Any]] = None,
) -> Any:
    """Perform an authenticated request against the Pennylane Company v2 API."""
    key = _resolve_company(company)
    token = COMPANIES[key]
    url = f"{API_BASE_URL}/{path.lstrip('/')}"
    headers = _headers(token, json_content=files is None)
    return await _send(method, url, headers, path, f"company '{key}'",
                       params, json_body, files)


async def _firm_request(
    method: str,
    path: str,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[Any] = None,
    files: Optional[dict[str, Any]] = None,
) -> Any:
    """Perform an authenticated request against the Pennylane Firm v1 API.

    Uses the firm token and firm base URL; the 2026 opt-in header is never sent
    (it only concerns Company v2).
    """
    if not FIRM_TOKEN:
        raise PennylaneError(
            "No Pennylane firm token is configured. Set PENNYLANE_FIRM_API_KEY "
            "in the environment (generate one in Pennylane: Firm settings -> "
            "Firm Tokens). See the README."
        )
    url = f"{FIRM_API_BASE_URL}/{path.lstrip('/')}"
    headers = _headers(FIRM_TOKEN, json_content=files is None, use_2026=False)
    return await _send(method, url, headers, path, "the firm token",
                       params, json_body, files)


def _resolve_firm_company_id(company_id: Optional[int]) -> int:
    """Pick the client company (dossier) a firm call targets, or raise."""
    if company_id is not None:
        return company_id
    if FIRM_DEFAULT_COMPANY_ID is not None:
        return FIRM_DEFAULT_COMPANY_ID
    raise PennylaneError(
        "No company_id given. Firm API calls target one client company: pass "
        "`company_id` explicitly (find IDs with pennylane_firm_list_companies) "
        "or set PENNYLANE_FIRM_DEFAULT_COMPANY_ID."
    )


def _firm_company_path(company_id: Optional[int], suffix: str) -> str:
    """Build a company-scoped firm path: companies/{id}/{suffix}."""
    return f"companies/{_resolve_firm_company_id(company_id)}/{suffix.lstrip('/')}"


def _describe_http_error(resp: httpx.Response, auth_desc: str) -> str:
    """Translate an HTTP error into an actionable, non-leaky message."""
    code = resp.status_code
    detail = ""
    try:
        body = resp.json()
        detail = body.get("error") or body.get("message") or json.dumps(body)
    except ValueError:
        detail = (resp.text or "").strip()[:300]

    base = {
        400: "Bad request — check filters, date formats (YYYY-MM-DD) and that "
             "numeric amounts are sent as strings.",
        401: f"Authentication failed for {auth_desc}. The API token "
             "is missing, invalid, or expired.",
        403: "Permission denied — the token is missing the required scope for "
             "this endpoint (e.g. customer_invoices:readonly).",
        404: "Not found — verify the resource ID (v2 uses Pennylane internal IDs).",
        409: "Conflict — the resource may not be ready yet (e.g. invoice PDF "
             "still generating). Retry shortly.",
        422: "Unprocessable — the payload failed validation. Check required "
             "fields and value formats.",
        429: "Rate limit exceeded (25 requests / 5s per token). Slow down.",
    }.get(code, f"API request failed with HTTP {code}.")

    return f"Error {code}: {base}" + (f" Detail: {detail}" if detail else "")


def _build_list_params(
    filters: Optional[list[dict[str, Any]]],
    cursor: Optional[str],
    limit: Optional[int],
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Assemble query params for a listing endpoint (filter + cursor pagination)."""
    params: dict[str, Any] = {}
    if filters:
        params["filter"] = json.dumps(filters)
    if cursor:
        params["cursor"] = cursor
    if limit is not None:
        params["limit"] = limit
    if extra:
        params.update({k: v for k, v in extra.items() if v is not None})
    return params


def _format(data: Any, fmt: ResponseFormat) -> str:
    """Render a tool result as JSON (default) or a compact Markdown summary."""
    if fmt == ResponseFormat.JSON:
        return json.dumps(data, indent=2, ensure_ascii=False)

    # Markdown best-effort summary for list-style payloads.
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        items = data["items"]
        lines = [f"**{len(items)} item(s)** — has_more: {data.get('has_more', False)}"]
        if data.get("next_cursor"):
            lines.append(f"next_cursor: `{data['next_cursor']}`")
        lines.append("")
        for it in items:
            if isinstance(it, dict):
                ident = it.get("id") or it.get("number") or it.get("formatted_number") or "?"
                label = (
                    it.get("label") or it.get("name") or it.get("invoice_number")
                    or it.get("reference") or it.get("company_name") or ""
                )
                money = it.get("currency_amount") or it.get("amount") or it.get("debits")
                date = it.get("date") or it.get("created_at") or ""
                bits = [f"**{ident}**", str(label)]
                if money:
                    bits.append(f"amt={money}")
                if date:
                    bits.append(str(date))
                lines.append("- " + " · ".join(b for b in bits if b))
            else:
                lines.append(f"- {it}")
        return "\n".join(lines)

    return json.dumps(data, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Shared input models
# --------------------------------------------------------------------------- #

class _Base(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class ListInput(_Base):
    """Common inputs for listing endpoints (filter + cursor pagination)."""
    company: Optional[str] = Field(
        default=None,
        description="Which configured company to query (e.g. 'acme', 'beta'). "
                    "Omit to use the default company.",
    )
    filters: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description="Pennylane filter array. Each item is "
                    "{field, operator, value}. Operators: eq, not_eq, lt, lteq, "
                    "gt, gteq, in, not_in, start_with. Example: "
                    '[{"field":"date","operator":"gteq","value":"2026-01-01"}]',
    )
    cursor: Optional[str] = Field(
        default=None,
        description="Opaque pagination cursor from a previous response's next_cursor.",
    )
    limit: Optional[int] = Field(
        default=20, ge=1, le=1000,
        description="Max items to return (default 20; per-endpoint max up to 1000).",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.JSON,
        description="'json' (default, full fidelity) or 'markdown' (compact summary).",
    )


class CompanyOnly(_Base):
    company: Optional[str] = Field(
        default=None, description="Which configured company to use. Omit for default."
    )


class GetByIdInput(_Base):
    id: str = Field(..., description="Pennylane internal ID of the resource.", min_length=1)
    company: Optional[str] = Field(
        default=None, description="Which configured company to use. Omit for default."
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)


# --------------------------------------------------------------------------- #
# Configuration / connectivity tools
# --------------------------------------------------------------------------- #

@mcp.tool(
    name="pennylane_list_companies",
    annotations={"title": "List configured Pennylane companies", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_list_companies() -> str:
    """List the Pennylane companies configured in this server (names only).

    Tokens are never returned. Use the returned names as the `company` argument
    on other tools.

    Returns:
        str: JSON {"companies": [str], "default": str|null, "count": int}.
    """
    default = DEFAULT_COMPANY if (DEFAULT_COMPANY in COMPANIES) else (
        next(iter(COMPANIES)) if len(COMPANIES) == 1 else None
    )
    return json.dumps(
        {"companies": sorted(COMPANIES), "default": default, "count": len(COMPANIES)},
        indent=2,
    )


@mcp.tool(
    name="pennylane_whoami",
    annotations={"title": "Verify token / show account (GET /me)", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_whoami(params: CompanyOnly) -> str:
    """Verify a company's API token and return the associated user/company (GET /me).

    Use this first to confirm credentials and environment (sandbox vs production)
    are set up correctly.

    Args:
        params.company (Optional[str]): Which configured company to check.

    Returns:
        str: JSON profile, e.g. {"id","email","role", ...}, or an error string.
    """
    try:
        return json.dumps(await _request("GET", "me", params.company), indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


# --------------------------------------------------------------------------- #
# Sales: customer invoices, customers, products, quotes
# --------------------------------------------------------------------------- #

@mcp.tool(
    name="pennylane_list_customer_invoices",
    annotations={"title": "List customer invoices", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_list_customer_invoices(params: ListInput) -> str:
    """List customer (sales) invoices and credit notes.

    Server-side filter fields: id, date, invoice_number, customer_id,
    billing_subscription_id, quote_id, draft, external_reference. NOTE: `paid`
    and `status` are NOT server-filterable — fetch (optionally date-bounded) then
    filter client-side on the `paid` boolean. Booleans take STRING values, e.g.
    {"field":"draft","operator":"eq","value":"false"}. Example — since Jan:
    [{"field":"date","operator":"gteq","value":"2026-01-01"}]. Each item returns
    amount & currency_amount (amount is EUR-normalized), paid, status, deadline,
    and remaining_amount_with_tax (EUR; the outstanding receivable).

    Args:
        params (ListInput): company, filters, cursor, limit, response_format.

    Returns:
        str: {"items":[...], "has_more":bool, "next_cursor":str|null} as JSON,
             or a Markdown summary.
    """
    try:
        data = await _request("GET", "customer_invoices", params.company,
                              _build_list_params(params.filters, params.cursor, params.limit))
        return _format(data, params.response_format)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_get_customer_invoice",
    annotations={"title": "Get a customer invoice", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_get_customer_invoice(params: GetByIdInput) -> str:
    """Retrieve a single customer invoice or credit note by its internal ID.

    Args:
        params.id (str): Pennylane internal invoice ID.
        params.company (Optional[str]): Which configured company to use.

    Returns:
        str: JSON invoice object (amounts as strings), or an error string.
    """
    try:
        data = await _request("GET", f"customer_invoices/{params.id}", params.company)
        return _format(data, params.response_format)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_list_customers",
    annotations={"title": "List customers", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_list_customers(params: ListInput) -> str:
    """List customers (both company and individual).

    Common filterable fields: name, reg_no (SIREN/SIRET), vat_number, emails,
    created_at. Example: [{"field":"name","operator":"start_with","value":"Acme"}].

    Returns:
        str: {"items":[...], "has_more":bool, "next_cursor":str|null}.
    """
    try:
        data = await _request("GET", "customers", params.company,
                              _build_list_params(params.filters, params.cursor, params.limit))
        return _format(data, params.response_format)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_list_products",
    annotations={"title": "List products", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_list_products(params: ListInput) -> str:
    """List products / services used on invoices and quotes.

    Common filterable fields: label, reference, vat_rate, created_at.

    Returns:
        str: {"items":[...], "has_more":bool, "next_cursor":str|null}.
    """
    try:
        data = await _request("GET", "products", params.company,
                              _build_list_params(params.filters, params.cursor, params.limit))
        return _format(data, params.response_format)
    except PennylaneError as exc:
        return str(exc)


# --------------------------------------------------------------------------- #
# Purchases: supplier invoices, suppliers
# --------------------------------------------------------------------------- #

@mcp.tool(
    name="pennylane_list_supplier_invoices",
    annotations={"title": "List supplier invoices", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_list_supplier_invoices(params: ListInput) -> str:
    """List supplier (purchase) invoices.

    Server-side filter fields: id, supplier_id, invoice_number, date,
    external_reference, payment_status. NOTE: there is no `paid`/`deadline`
    filter — fetch (optionally date-bounded) then filter client-side on the
    `paid` boolean and `deadline`. Example — since Jan:
    [{"field":"date","operator":"gteq","value":"2026-01-01"}]. Each item returns
    amount (EUR-normalized), currency_amount, paid, payment_status, deadline,
    and remaining_amount_with_tax (EUR; negative = still owed to the supplier).

    Returns:
        str: {"items":[...], "has_more":bool, "next_cursor":str|null}.
    """
    try:
        data = await _request("GET", "supplier_invoices", params.company,
                              _build_list_params(params.filters, params.cursor, params.limit))
        return _format(data, params.response_format)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_get_supplier_invoice",
    annotations={"title": "Get a supplier invoice", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_get_supplier_invoice(params: GetByIdInput) -> str:
    """Retrieve a single supplier invoice by its internal ID.

    Args:
        params.id (str): Pennylane internal supplier-invoice ID.

    Returns:
        str: JSON supplier invoice object, or an error string.
    """
    try:
        data = await _request("GET", f"supplier_invoices/{params.id}", params.company)
        return _format(data, params.response_format)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_list_suppliers",
    annotations={"title": "List suppliers", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_list_suppliers(params: ListInput) -> str:
    """List suppliers (vendors).

    Common filterable fields: name, reg_no, vat_number, created_at.

    Returns:
        str: {"items":[...], "has_more":bool, "next_cursor":str|null}.
    """
    try:
        data = await _request("GET", "suppliers", params.company,
                              _build_list_params(params.filters, params.cursor, params.limit))
        return _format(data, params.response_format)
    except PennylaneError as exc:
        return str(exc)


# --------------------------------------------------------------------------- #
# Banking & ledger
# --------------------------------------------------------------------------- #

@mcp.tool(
    name="pennylane_list_transactions",
    annotations={"title": "List bank transactions", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_list_transactions(params: ListInput) -> str:
    """List banking transactions across connected bank accounts.

    Common filterable fields: date, label, amount, currency, bank_account_id.

    Returns:
        str: {"items":[...], "has_more":bool, "next_cursor":str|null}.
    """
    try:
        data = await _request("GET", "transactions", params.company,
                              _build_list_params(params.filters, params.cursor, params.limit))
        return _format(data, params.response_format)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_list_ledger_entries",
    annotations={"title": "List ledger entries", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_list_ledger_entries(params: ListInput) -> str:
    """List accounting ledger entries (journal entries).

    Tip: for ongoing reporting prefer a full export + the changelog endpoints
    rather than repeatedly listing all entries (can time out on large books).
    Common filterable fields: date, journal_id, label.

    Returns:
        str: {"items":[...], "has_more":bool, "next_cursor":str|null}.
    """
    try:
        data = await _request("GET", "ledger_entries", params.company,
                              _build_list_params(params.filters, params.cursor, params.limit))
        return _format(data, params.response_format)
    except PennylaneError as exc:
        return str(exc)


class TrialBalanceInput(_Base):
    """Inputs for the trial balance report."""
    period_start: str = Field(..., description="Start of period, YYYY-MM-DD (e.g. 2026-01-01).")
    period_end: str = Field(..., description="End of period, YYYY-MM-DD (e.g. 2026-12-31).")
    company: Optional[str] = Field(default=None, description="Which configured company.")
    is_auxiliary: Optional[bool] = Field(
        default=None, description="Include auxiliary (sub-)accounts."
    )
    cursor: Optional[str] = Field(default=None, description="Pagination cursor.")
    limit: Optional[int] = Field(default=100, ge=1, le=1000, description="Items per page.")
    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)


@mcp.tool(
    name="pennylane_get_trial_balance",
    annotations={"title": "Get trial balance", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_get_trial_balance(params: TrialBalanceInput) -> str:
    """Get the trial balance (balance générale) for a period.

    Returns balances grouped by ledger account: number, formatted_number, label,
    debits, credits (amounts are strings). Requires the trial_balance:readonly scope.

    Args:
        params.period_start (str): Period start, YYYY-MM-DD.
        params.period_end (str): Period end, YYYY-MM-DD.
        params.is_auxiliary (Optional[bool]): Include auxiliary accounts.

    Returns:
        str: {"items":[{number,formatted_number,label,debits,credits}],
              "has_more":bool, "next_cursor":str|null}.
    """
    try:
        extra = {
            "period_start": params.period_start,
            "period_end": params.period_end,
            "is_auxiliary": params.is_auxiliary,
        }
        query = _build_list_params(None, params.cursor, params.limit, extra)
        data = await _request("GET", "trial_balance", params.company, query)
        return _format(data, params.response_format)
    except PennylaneError as exc:
        return str(exc)


# --------------------------------------------------------------------------- #
# Generic read-only escape hatch (covers every GET endpoint)
# --------------------------------------------------------------------------- #

class GenericGetInput(_Base):
    """Inputs for an arbitrary read-only GET against the v2 API."""
    path: str = Field(
        ...,
        description="API path under /api/external/v2, e.g. 'quotes', "
                    "'ledger_accounts', 'customer_invoices/123/payments', 'journals'. "
                    "Do not include the host or the /api/external/v2 prefix.",
        min_length=1,
    )
    company: Optional[str] = Field(default=None, description="Which configured company.")
    filters: Optional[list[dict[str, Any]]] = Field(
        default=None, description="Optional filter array ({field,operator,value})."
    )
    query: Optional[dict[str, Any]] = Field(
        default=None, description="Additional raw query params (e.g. {'cursor':'..','limit':50})."
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)


@mcp.tool(
    name="pennylane_get",
    annotations={"title": "Generic GET (any v2 endpoint)", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_get(params: GenericGetInput) -> str:
    """Read-only access to ANY Company API v2 GET endpoint not covered by a
    dedicated tool (quotes, ledger_accounts, journals, categories, bank_accounts,
    billing_subscriptions, *_invoices/{id}/payments, *_changes changelogs, ...).

    This is the universal fallback so the agent can reach the full read surface
    of the API. Only GET requests are issued — it can never modify data.

    Args:
        params.path (str): Path under /api/external/v2 (no leading host/prefix).
        params.filters (Optional[list]): Filter array, applied as `filter`.
        params.query (Optional[dict]): Extra query params (cursor, limit, dates...).

    Returns:
        str: Raw JSON response from the endpoint, or an error string.

    Examples:
        - List quotes: path="quotes"
        - A ledger account: path="ledger_accounts/123"
        - Invoice payments: path="customer_invoices/123/payments"
        - Customer changelog: path="customers/changes", query={"start_date":"2026-06-01"}
    """
    try:
        merged = dict(params.query or {})
        if params.filters:
            merged["filter"] = json.dumps(params.filters)
        data = await _request("GET", params.path, params.company, merged or None)
        return _format(data, params.response_format)
    except PennylaneError as exc:
        return str(exc)


# --------------------------------------------------------------------------- #
# Write tools (kept narrow & safe: create-only; nothing is finalized/deleted)
# --------------------------------------------------------------------------- #

class CreateIndividualCustomerInput(_Base):
    """Create an individual (person) customer."""
    first_name: str = Field(..., description="Customer first name.", min_length=1)
    last_name: str = Field(..., description="Customer last name.", min_length=1)
    company: Optional[str] = Field(default=None, description="Which configured company.")
    email: Optional[str] = Field(default=None, description="Customer email.")
    extra_fields: Optional[dict[str, Any]] = Field(
        default=None,
        description="Any additional API fields (address, billing_address, "
                    "phone, etc.) passed straight through to the create payload.",
    )


@mcp.tool(
    name="pennylane_create_individual_customer",
    annotations={"title": "Create an individual customer", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_create_individual_customer(params: CreateIndividualCustomerInput) -> str:
    """Create an individual (person) customer. Requires the customers:all scope.

    Args:
        params.first_name (str), params.last_name (str): Required.
        params.email (Optional[str]): Optional contact email.
        params.extra_fields (Optional[dict]): Extra API fields passed through.

    Returns:
        str: JSON of the created customer (including its new internal id).
    """
    try:
        payload: dict[str, Any] = {
            "first_name": params.first_name,
            "last_name": params.last_name,
        }
        if params.email:
            payload["email"] = params.email
        if params.extra_fields:
            payload.update(params.extra_fields)
        data = await _request("POST", "customers/individual", params.company, json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class CreateCompanyCustomerInput(_Base):
    """Create a company (business) customer."""
    name: str = Field(..., description="Legal/company name.", min_length=1)
    company: Optional[str] = Field(default=None, description="Which configured company.")
    reg_no: Optional[str] = Field(default=None, description="SIREN/SIRET registration number.")
    vat_number: Optional[str] = Field(default=None, description="Intra-community VAT number.")
    emails: Optional[list[str]] = Field(default=None, description="Billing email(s).")
    extra_fields: Optional[dict[str, Any]] = Field(
        default=None, description="Additional API fields passed through (address, etc.)."
    )


@mcp.tool(
    name="pennylane_create_company_customer",
    annotations={"title": "Create a company customer", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_create_company_customer(params: CreateCompanyCustomerInput) -> str:
    """Create a company (business) customer. Requires the customers:all scope.

    Args:
        params.name (str): Required company name.
        params.reg_no / vat_number / emails (Optional): Common identifiers.
        params.extra_fields (Optional[dict]): Extra API fields passed through.

    Returns:
        str: JSON of the created customer (including its new internal id).
    """
    try:
        payload: dict[str, Any] = {"name": params.name}
        if params.reg_no:
            payload["reg_no"] = params.reg_no
        if params.vat_number:
            payload["vat_number"] = params.vat_number
        if params.emails:
            payload["emails"] = params.emails
        if params.extra_fields:
            payload.update(params.extra_fields)
        data = await _request("POST", "customers/company", params.company, json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class CreateSupplierInput(_Base):
    """Create a supplier (vendor)."""
    name: str = Field(..., description="Supplier / legal name.", min_length=1)
    company: Optional[str] = Field(default=None, description="Which configured company.")
    reg_no: Optional[str] = Field(default=None, description="SIREN/SIRET registration number.")
    vat_number: Optional[str] = Field(default=None, description="Intra-community VAT number.")
    emails: Optional[list[str]] = Field(default=None, description="Contact email(s).")
    extra_fields: Optional[dict[str, Any]] = Field(
        default=None, description="Additional API fields passed through (address, iban, etc.)."
    )


@mcp.tool(
    name="pennylane_create_supplier",
    annotations={"title": "Create a supplier", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_create_supplier(params: CreateSupplierInput) -> str:
    """Create a supplier (vendor). Requires the suppliers:all scope.

    Use this before pennylane_import_supplier_invoice when the supplier does not
    exist yet — the import needs an existing supplier_id.

    Args:
        params.name (str): Required supplier name.
        params.reg_no / vat_number / emails (Optional): Common identifiers.
        params.extra_fields (Optional[dict]): Extra API fields passed through.

    Returns:
        str: JSON of the created supplier (including its new internal id).
    """
    try:
        payload: dict[str, Any] = {"name": params.name}
        if params.reg_no:
            payload["reg_no"] = params.reg_no
        if params.vat_number:
            payload["vat_number"] = params.vat_number
        if params.emails:
            payload["emails"] = params.emails
        if params.extra_fields:
            payload.update(params.extra_fields)
        data = await _request("POST", "suppliers", params.company, json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class CreateDraftInvoiceInput(_Base):
    """Create a DRAFT customer invoice (never auto-finalized)."""
    customer_id: int = Field(..., description="Internal ID of an existing customer.")
    date: str = Field(..., description="Invoice date, YYYY-MM-DD.")
    company: Optional[str] = Field(default=None, description="Which configured company.")
    deadline: Optional[str] = Field(default=None, description="Payment due date, YYYY-MM-DD.")
    currency: Optional[str] = Field(default="EUR", description="ISO currency code (default EUR).")
    invoice_lines: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description="Line items. Each line is an object per the API, e.g. "
                    '{"label":"Consulting","quantity":2,"unit":"hour",'
                    '"raw_currency_unit_price":"100.00","vat_rate":"FR_200"} '
                    "(or use \"product_id\" instead of label/price). Monetary "
                    "amounts MUST be strings. VAT codes: FR_200, FR_100, FR_055, "
                    "exempt, any.",
    )
    extra_fields: Optional[dict[str, Any]] = Field(
        default=None, description="Any other create fields passed straight through."
    )


@mcp.tool(
    name="pennylane_create_draft_customer_invoice",
    annotations={"title": "Create a DRAFT customer invoice", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_create_draft_customer_invoice(params: CreateDraftInvoiceInput) -> str:
    """Create a DRAFT customer invoice (safe: NOT finalized, no legal invoice number yet).

    The customer must already exist (v2 requires creating resources separately) —
    use pennylane_create_company_customer / _individual_customer first if needed.
    Remember Pennylane v2 expects monetary amounts as STRINGS.

    Args:
        params.customer_id (int): Existing customer's internal ID.
        params.date (str): Invoice date YYYY-MM-DD.
        params.invoice_lines (Optional[list]): Line items (amounts as strings).
        params.extra_fields (Optional[dict]): Extra create fields.

    Returns:
        str: JSON of the created draft invoice (with its internal id). Finalize it
             later in the Pennylane UI or via the finalize endpoint when ready.
    """
    try:
        payload: dict[str, Any] = {
            "draft": True,
            "customer_id": params.customer_id,
            "date": params.date,
            "currency": params.currency or "EUR",
        }
        if params.deadline:
            payload["deadline"] = params.deadline
        if params.invoice_lines:
            payload["invoice_lines"] = params.invoice_lines
        if params.extra_fields:
            payload.update(params.extra_fields)
        data = await _request("POST", "customer_invoices", params.company, json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


# --------------------------------------------------------------------------- #
# Accounting reads: ledger accounts & journals (round out reporting)
# --------------------------------------------------------------------------- #

@mcp.tool(
    name="pennylane_list_ledger_accounts",
    annotations={"title": "List ledger accounts (chart of accounts)", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_list_ledger_accounts(params: ListInput) -> str:
    """List the company's ledger accounts (plan comptable / chart of accounts).

    Useful to resolve account IDs for invoice imports and ledger entries.
    Common filterable fields: number, label. Example (find revenue accounts):
    [{"field":"number","operator":"start_with","value":"706"}].

    Returns:
        str: {"items":[...], "has_more":bool, "next_cursor":str|null}.
    """
    try:
        data = await _request("GET", "ledger_accounts", params.company,
                              _build_list_params(params.filters, params.cursor, params.limit))
        return _format(data, params.response_format)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_list_journals",
    annotations={"title": "List accounting journals", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_list_journals(params: ListInput) -> str:
    """List accounting journals (e.g. sales, purchases, bank).

    Returns:
        str: {"items":[...], "has_more":bool, "next_cursor":str|null}.
    """
    try:
        data = await _request("GET", "journals", params.company,
                              _build_list_params(params.filters, params.cursor, params.limit))
        return _format(data, params.response_format)
    except PennylaneError as exc:
        return str(exc)


# --------------------------------------------------------------------------- #
# Accounting exports: FEC / general ledger / analytical general ledger
# --------------------------------------------------------------------------- #

class ExportKind(str, Enum):
    """Which accounting export to generate."""
    FEC = "fec"
    GENERAL_LEDGER = "general_ledger"
    ANALYTICAL_GENERAL_LEDGER = "analytical_general_ledger"


# Export creation endpoints are pluralized on the v2 API.
_EXPORT_PATHS = {
    ExportKind.FEC: "exports/fecs",
    ExportKind.GENERAL_LEDGER: "exports/general_ledgers",
    ExportKind.ANALYTICAL_GENERAL_LEDGER: "exports/analytical_general_ledgers",
}


class CreateExportInput(_Base):
    """Trigger an accounting export for a period."""
    kind: ExportKind = Field(
        ...,
        description="Which export to generate: 'fec' (Fichier des Écritures "
                    "Comptables), 'general_ledger', or 'analytical_general_ledger'.",
    )
    period_start: str = Field(..., description="Start of period, YYYY-MM-DD.")
    period_end: str = Field(..., description="End of period, YYYY-MM-DD.")
    company: Optional[str] = Field(default=None, description="Which configured company.")
    mode: Optional[str] = Field(
        default=None,
        description="Only for analytical_general_ledger: 'in_line' (default) or "
                    "'in_column'. Ignored for other export kinds.",
    )
    extra_fields: Optional[dict[str, Any]] = Field(
        default=None, description="Any other export fields passed straight through."
    )


@mcp.tool(
    name="pennylane_create_export",
    annotations={"title": "Create an accounting export (FEC / general ledger)",
                 "readOnlyHint": False, "destructiveHint": False,
                 "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_create_export(params: CreateExportInput) -> str:
    """Trigger an accounting export (FEC, general ledger, or analytical general ledger).

    Exports are asynchronous: this returns an export object with an `id` and a
    `status`. Poll it with pennylane_get (path e.g. `exports/fecs/{id}`,
    `exports/general_ledgers/{id}`, `exports/analytical_general_ledgers/{id}`)
    until the download URL is available. This is the preferred path for full-book
    reporting instead of repeatedly listing ledger entries. Requires the matching
    export scope (e.g. exports:fec).

    Args:
        params.kind (ExportKind): fec / general_ledger / analytical_general_ledger.
        params.period_start (str), params.period_end (str): Period, YYYY-MM-DD.
        params.mode (Optional[str]): 'in_line'/'in_column' (analytical only).

    Returns:
        str: JSON of the created export (with its `id` and `status`).
    """
    try:
        payload: dict[str, Any] = {
            "period_start": params.period_start,
            "period_end": params.period_end,
        }
        if params.kind == ExportKind.ANALYTICAL_GENERAL_LEDGER and params.mode:
            payload["mode"] = params.mode
        if params.extra_fields:
            payload.update(params.extra_fields)
        data = await _request("POST", _EXPORT_PATHS[params.kind], params.company, json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


# --------------------------------------------------------------------------- #
# Customer-invoice lifecycle: finalize, send, mark paid
# --------------------------------------------------------------------------- #

@mcp.tool(
    name="pennylane_finalize_customer_invoice",
    annotations={"title": "Finalize a draft customer invoice", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_finalize_customer_invoice(params: GetByIdInput) -> str:
    """Finalize a DRAFT customer invoice (PUT /customer_invoices/{id}/finalize).

    This assigns the legal invoice number and locks the document — once finalized
    it can NO LONGER be edited. Treat as irreversible; confirm with the user first.
    Requires the customer_invoices:all scope.

    Args:
        params.id (str): Internal ID of the draft invoice to finalize.

    Returns:
        str: JSON of the finalized invoice (now with invoice_number, draft=false).
    """
    try:
        data = await _request("POST", f"customer_invoices/{params.id}/finalize", params.company)
        return _format(data, params.response_format)
    except PennylaneError as exc:
        return str(exc)


class SendInvoiceEmailInput(_Base):
    """Send a finalized customer invoice by email."""
    id: str = Field(..., description="Internal ID of the finalized invoice.", min_length=1)
    company: Optional[str] = Field(default=None, description="Which configured company.")
    recipients: Optional[list[str]] = Field(
        default=None,
        description="Email addresses to send to. If omitted, Pennylane uses the "
                    "customer's billing email(s).",
    )


@mcp.tool(
    name="pennylane_send_customer_invoice_by_email",
    annotations={"title": "Send a customer invoice by email", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_send_customer_invoice_by_email(params: SendInvoiceEmailInput) -> str:
    """Email a finalized customer invoice to the customer
    (POST /customer_invoices/{id}/send_by_email).

    The invoice must be finalized and its PDF generated — if you just finalized it,
    the API may briefly return 409 while the PDF renders; retry in a minute.
    Requires the customer_invoices:all scope.

    Args:
        params.id (str): Internal ID of the finalized invoice.
        params.recipients (Optional[list[str]]): Override recipient emails.

    Returns:
        str: Confirmation that the email is on its way, or an error string.
    """
    try:
        body = {"recipients": params.recipients} if params.recipients else None
        data = await _request("POST", f"customer_invoices/{params.id}/send_by_email",
                              params.company, json_body=body)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_mark_customer_invoice_as_paid",
    annotations={"title": "Mark a customer invoice as paid", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_mark_customer_invoice_as_paid(params: GetByIdInput) -> str:
    """Mark a customer invoice as paid (PUT /customer_invoices/{id}/mark_as_paid).

    Note: this only flips the paid status — it does NOT reconcile the invoice with a
    bank transaction. To actually reconcile, use
    pennylane_match_customer_invoice_transaction instead. Requires customer_invoices:all.

    Args:
        params.id (str): Internal ID of the invoice.

    Returns:
        str: Confirmation (HTTP 204) or an error string.
    """
    try:
        data = await _request("POST", f"customer_invoices/{params.id}/mark_as_paid", params.company)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


# --------------------------------------------------------------------------- #
# Reconciliation: match a bank transaction to an invoice
# --------------------------------------------------------------------------- #

class MatchTransactionInput(_Base):
    """Match an existing bank transaction to an existing (non-draft) invoice."""
    invoice_id: int = Field(..., description="Internal ID of the invoice (not a draft).")
    transaction_id: int = Field(..., description="Internal ID of the bank transaction.")
    company: Optional[str] = Field(default=None, description="Which configured company.")


@mcp.tool(
    name="pennylane_match_customer_invoice_transaction",
    annotations={"title": "Match a transaction to a customer invoice", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_match_customer_invoice_transaction(params: MatchTransactionInput) -> str:
    """Reconcile a bank transaction with a customer invoice
    (POST /customer_invoices/{invoice_id}/matched_transactions, body {transaction_id}).

    One transaction is matched per call; not applicable to draft invoices.
    Requires the customer_invoices:all scope.

    Args:
        params.invoice_id (int): The customer invoice's internal ID.
        params.transaction_id (int): The bank transaction's internal ID.

    Returns:
        str: Confirmation (HTTP 204) or an error string.
    """
    try:
        data = await _request(
            "POST", f"customer_invoices/{params.invoice_id}/matched_transactions",
            params.company, json_body={"transaction_id": params.transaction_id},
        )
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_match_supplier_invoice_transaction",
    annotations={"title": "Match a transaction to a supplier invoice", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_match_supplier_invoice_transaction(params: MatchTransactionInput) -> str:
    """Reconcile a bank transaction with a supplier invoice
    (POST /supplier_invoices/{invoice_id}/matched_transactions, body {transaction_id}).

    One transaction is matched per call. Requires the supplier_invoices:all scope.

    Args:
        params.invoice_id (int): The supplier invoice's internal ID.
        params.transaction_id (int): The bank transaction's internal ID.

    Returns:
        str: Confirmation (HTTP 204) or an error string.
    """
    try:
        data = await _request(
            "POST", f"supplier_invoices/{params.invoice_id}/matched_transactions",
            params.company, json_body={"transaction_id": params.transaction_id},
        )
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


# --------------------------------------------------------------------------- #
# Expense import pipeline: upload a PDF, then import the supplier invoice
# --------------------------------------------------------------------------- #

class UploadFileInput(_Base):
    """Upload a local PDF to Pennylane as a file_attachment."""
    file_path: str = Field(..., description="Absolute path to a local PDF file.", min_length=1)
    company: Optional[str] = Field(default=None, description="Which configured company.")


@mcp.tool(
    name="pennylane_upload_file_attachment",
    annotations={"title": "Upload a PDF file attachment", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_upload_file_attachment(params: UploadFileInput) -> str:
    """Upload a local PDF to Pennylane (multipart POST /file_attachments).

    Returns a file_attachment with an `id` to pass as `file_attachment_id` when
    importing a supplier or customer invoice. PDF only, up to 100 MB. Requires the
    file_attachments:all scope.

    Args:
        params.file_path (str): Absolute path to a local PDF on this machine.

    Returns:
        str: JSON {"id", "filename", "status"} of the uploaded attachment.
    """
    try:
        p = Path(params.file_path).expanduser()
        if not p.is_file():
            return f"Error: no file found at '{params.file_path}'. Provide an absolute path to a PDF."
        files = {"file": (p.name, p.read_bytes(), "application/pdf")}
        data = await _request("POST", "file_attachments", params.company, files=files)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class ImportSupplierInvoiceInput(_Base):
    """Import a supplier invoice from an already-uploaded PDF."""
    file_attachment_id: int = Field(..., description="ID from pennylane_upload_file_attachment.")
    date: str = Field(..., description="Invoice date, YYYY-MM-DD.")
    company: Optional[str] = Field(default=None, description="Which configured company.")
    supplier_id: Optional[int] = Field(
        default=None,
        description="Internal ID of the supplier. If it doesn't exist yet, create "
                    "it first with pennylane_create_supplier.",
    )
    deadline: Optional[str] = Field(default=None, description="Payment due date, YYYY-MM-DD.")
    currency_amount: Optional[str] = Field(
        default=None, description="Total incl. tax, as a STRING e.g. \"120.00\"."
    )
    currency_amount_before_tax: Optional[str] = Field(
        default=None, description="Total excl. tax, as a STRING e.g. \"100.00\"."
    )
    currency_tax: Optional[str] = Field(
        default=None, description="Total VAT, as a STRING e.g. \"20.00\"."
    )
    invoice_lines: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description="Optional line items, e.g. "
                    '[{"ledger_account_id":601002,"currency_amount":"120.00",'
                    '"currency_tax":"20.00","vat_rate":"FR_200"}]. The sum of line '
                    "currency_amount must equal the total currency_amount. Omit "
                    "ledger_account_id to use the company's default mapping.",
    )
    extra_fields: Optional[dict[str, Any]] = Field(
        default=None, description="Any other import fields passed straight through."
    )


@mcp.tool(
    name="pennylane_import_supplier_invoice",
    annotations={"title": "Import a supplier invoice (from uploaded PDF)", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_import_supplier_invoice(params: ImportSupplierInvoiceInput) -> str:
    """Import a supplier (purchase) invoice from a previously uploaded PDF
    (POST /supplier_invoices/import).

    Workflow: pennylane_upload_file_attachment -> use its id here. Amounts are
    STRINGS; the sum of invoice_lines.currency_amount must equal currency_amount,
    or the API returns 422. Re-importing the same PDF is rejected as a duplicate.
    Requires the supplier_invoices:all (and file_attachments:all) scopes.

    Args:
        params.file_attachment_id (int): Uploaded PDF's attachment id.
        params.date (str): Invoice date YYYY-MM-DD.
        params.supplier_id (Optional[int]): Supplier internal ID.
        params.currency_amount* (Optional[str]): Totals as strings.
        params.invoice_lines (Optional[list]): Accounting line items.

    Returns:
        str: JSON of the imported supplier invoice (with its internal id), or an error.
    """
    try:
        payload: dict[str, Any] = {
            "file_attachment_id": params.file_attachment_id,
            "date": params.date,
        }
        for field in ("supplier_id", "deadline", "currency_amount",
                      "currency_amount_before_tax", "currency_tax", "invoice_lines"):
            value = getattr(params, field)
            if value is not None:
                payload[field] = value
        if params.extra_fields:
            payload.update(params.extra_fields)
        data = await _request("POST", "supplier_invoices/import", params.company, json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


# --------------------------------------------------------------------------- #
# Writes: updates & deletes (partial updates; only what you pass is changed)
# --------------------------------------------------------------------------- #

class UpdateByIdInput(_Base):
    """Update a resource: only the fields you provide are changed."""
    id: str = Field(..., description="Internal ID of the resource to update.", min_length=1)
    company: Optional[str] = Field(default=None, description="Which configured company.")
    fields: dict[str, Any] = Field(
        ...,
        description="Object of fields to change. Include only what you want to "
                    "modify — omitted fields are left as-is. Monetary amounts must "
                    "be sent as STRINGS (e.g. \"100.00\").",
    )


@mcp.tool(
    name="pennylane_update_customer_invoice",
    annotations={"title": "Update a draft customer invoice", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_update_customer_invoice(params: UpdateByIdInput) -> str:
    """Update a DRAFT customer invoice (PATCH /customer_invoices/{id}).

    Only draft invoices can be edited — once finalized an invoice is locked.
    Common editable fields: date, deadline, customer_id, currency, invoice_lines,
    external_reference. Requires the customer_invoices:all scope.

    Args:
        params.id (str): Draft invoice internal ID.
        params.fields (dict): Fields to change (amounts as strings).

    Returns:
        str: JSON of the updated invoice, or an error string.
    """
    try:
        data = await _request("PATCH", f"customer_invoices/{params.id}", params.company,
                              json_body=params.fields)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_delete_draft_customer_invoice",
    annotations={"title": "Delete a draft customer invoice", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_delete_draft_customer_invoice(params: GetByIdInput) -> str:
    """Delete a DRAFT customer invoice (DELETE /customer_invoices/{id}).

    Only drafts can be deleted — finalized invoices are permanent legal documents
    and cannot be removed. Irreversible; confirm with the user first. Requires the
    customer_invoices:all scope.

    Args:
        params.id (str): Draft invoice internal ID.

    Returns:
        str: Confirmation (HTTP 204) or an error string.
    """
    try:
        data = await _request("DELETE", f"customer_invoices/{params.id}", params.company)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_update_company_customer",
    annotations={"title": "Update a company customer", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_update_company_customer(params: UpdateByIdInput) -> str:
    """Update a company (business) customer (PUT /customers/company/{id}).

    Common fields: name, reg_no, vat_number, emails, address, billing_address.
    Requires the customers:all scope.

    Args:
        params.id (str): Company customer internal ID.
        params.fields (dict): Fields to change.

    Returns:
        str: JSON of the updated customer, or an error string.
    """
    try:
        data = await _request("PUT", f"customers/company/{params.id}", params.company,
                              json_body=params.fields)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_update_individual_customer",
    annotations={"title": "Update an individual customer", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_update_individual_customer(params: UpdateByIdInput) -> str:
    """Update an individual (person) customer (PUT /customers/individual/{id}).

    Common fields: first_name, last_name, email, address, billing_address.
    Requires the customers:all scope.

    Args:
        params.id (str): Individual customer internal ID.
        params.fields (dict): Fields to change.

    Returns:
        str: JSON of the updated customer, or an error string.
    """
    try:
        data = await _request("PUT", f"customers/individual/{params.id}", params.company,
                              json_body=params.fields)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_update_supplier",
    annotations={"title": "Update a supplier", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_update_supplier(params: UpdateByIdInput) -> str:
    """Update a supplier (PUT /suppliers/{id}).

    Common fields: name, reg_no, vat_number, emails, address, iban. Requires the
    suppliers:all scope.

    Args:
        params.id (str): Supplier internal ID.
        params.fields (dict): Fields to change.

    Returns:
        str: JSON of the updated supplier, or an error string.
    """
    try:
        data = await _request("PUT", f"suppliers/{params.id}", params.company,
                              json_body=params.fields)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_update_supplier_invoice",
    annotations={"title": "Update a supplier invoice", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_update_supplier_invoice(params: UpdateByIdInput) -> str:
    """Update a supplier (purchase) invoice (PUT /supplier_invoices/{id}).

    Common fields: date, deadline, supplier_id, currency_amount, invoice_lines,
    external_reference (amounts as strings). Requires the supplier_invoices:all scope.

    Args:
        params.id (str): Supplier invoice internal ID.
        params.fields (dict): Fields to change (amounts as strings).

    Returns:
        str: JSON of the updated supplier invoice, or an error string.
    """
    try:
        data = await _request("PUT", f"supplier_invoices/{params.id}", params.company,
                              json_body=params.fields)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class CreateProductInput(_Base):
    """Create a product / service used on invoices and quotes."""
    label: str = Field(..., description="Product/service label shown on documents.", min_length=1)
    company: Optional[str] = Field(default=None, description="Which configured company.")
    reference: Optional[str] = Field(default=None, description="SKU / internal reference.")
    unit: Optional[str] = Field(default=None, description="Unit label, e.g. 'hour', 'piece', 'day'.")
    vat_rate: Optional[str] = Field(
        default=None, description="VAT code, e.g. FR_200, FR_100, FR_055, exempt, any."
    )
    extra_fields: Optional[dict[str, Any]] = Field(
        default=None,
        description="Any other product fields passed straight through, e.g. "
                    "price_before_tax / raw_currency_unit_price (STRING amounts), "
                    "description, ledger_account_id.",
    )


@mcp.tool(
    name="pennylane_create_product",
    annotations={"title": "Create a product / service", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_create_product(params: CreateProductInput) -> str:
    """Create a product or service (POST /products). Requires the products:all scope.

    Monetary fields (unit price, etc.) go through extra_fields and must be STRINGS.

    Args:
        params.label (str): Required label.
        params.reference / unit / vat_rate (Optional): Common attributes.
        params.extra_fields (Optional[dict]): Pricing and any other fields.

    Returns:
        str: JSON of the created product (with its internal id), or an error string.
    """
    try:
        payload: dict[str, Any] = {"label": params.label}
        for field in ("reference", "unit", "vat_rate"):
            value = getattr(params, field)
            if value is not None:
                payload[field] = value
        if params.extra_fields:
            payload.update(params.extra_fields)
        data = await _request("POST", "products", params.company, json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_update_product",
    annotations={"title": "Update a product / service", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_update_product(params: UpdateByIdInput) -> str:
    """Update a product / service (PUT /products/{id}).

    Common fields: label, reference, unit, vat_rate, price (amounts as strings).
    Requires the products:all scope.

    Args:
        params.id (str): Product internal ID.
        params.fields (dict): Fields to change (amounts as strings).

    Returns:
        str: JSON of the updated product, or an error string.
    """
    try:
        data = await _request("PUT", f"products/{params.id}", params.company,
                              json_body=params.fields)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


# --------------------------------------------------------------------------- #
# Sales: quotes (create, update, status, send, convert to invoice)
# --------------------------------------------------------------------------- #

class CreateQuoteInput(_Base):
    """Create a quote (devis)."""
    customer_id: int = Field(..., description="Internal ID of an existing customer.")
    date: str = Field(..., description="Quote date, YYYY-MM-DD.")
    deadline: str = Field(..., description="Quote validity/expiry date, YYYY-MM-DD.")
    invoice_lines: list[dict[str, Any]] = Field(
        ...,
        description="Line items (at least one). Each line is either product-based "
                    '{"product_id":123,"quantity":2} or standard '
                    '{"label":"Consulting","quantity":2,"unit":"hour",'
                    '"raw_currency_unit_price":"100.00","vat_rate":"FR_200"}. '
                    "Monetary amounts MUST be strings. VAT codes: FR_200, FR_100, "
                    "FR_055, exempt, any.",
        min_length=1,
    )
    company: Optional[str] = Field(default=None, description="Which configured company.")
    currency: Optional[str] = Field(default="EUR", description="ISO currency code (default EUR).")
    extra_fields: Optional[dict[str, Any]] = Field(
        default=None, description="Any other create fields passed straight through."
    )


@mcp.tool(
    name="pennylane_create_quote",
    annotations={"title": "Create a quote", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_create_quote(params: CreateQuoteInput) -> str:
    """Create a quote / estimate (POST /quotes). Requires the quotes:all scope.

    The customer must already exist. Monetary amounts are STRINGS. date, deadline,
    customer_id and at least one invoice_line are required.

    Args:
        params.customer_id (int), params.date (str), params.deadline (str): Required.
        params.invoice_lines (list): At least one line (amounts as strings).
        params.extra_fields (Optional[dict]): Extra create fields.

    Returns:
        str: JSON of the created quote (with its internal id), or an error string.
    """
    try:
        payload: dict[str, Any] = {
            "customer_id": params.customer_id,
            "date": params.date,
            "deadline": params.deadline,
            "invoice_lines": params.invoice_lines,
            "currency": params.currency or "EUR",
        }
        if params.extra_fields:
            payload.update(params.extra_fields)
        data = await _request("POST", "quotes", params.company, json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_update_quote",
    annotations={"title": "Update a quote", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_update_quote(params: UpdateByIdInput) -> str:
    """Update a quote (PATCH /quotes/{id}).

    Common fields: date, deadline, customer_id, invoice_lines (amounts as strings).
    Requires the quotes:all scope.

    Args:
        params.id (str): Quote internal ID.
        params.fields (dict): Fields to change (amounts as strings).

    Returns:
        str: JSON of the updated quote, or an error string.
    """
    try:
        data = await _request("PATCH", f"quotes/{params.id}", params.company,
                              json_body=params.fields)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class QuoteStatus(str, Enum):
    """Allowed quote statuses."""
    PENDING = "pending"
    ACCEPTED = "accepted"
    DENIED = "denied"
    INVOICED = "invoiced"
    EXPIRED = "expired"


class QuoteStatusInput(_Base):
    """Update a quote's status."""
    id: str = Field(..., description="Quote internal ID.", min_length=1)
    status: QuoteStatus = Field(
        ..., description="New status: pending, accepted, denied, invoiced, or expired."
    )
    company: Optional[str] = Field(default=None, description="Which configured company.")


@mcp.tool(
    name="pennylane_update_quote_status",
    annotations={"title": "Update a quote's status", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_update_quote_status(params: QuoteStatusInput) -> str:
    """Set a quote's status (PUT /quotes/{id}/update_status).

    Statuses: pending, accepted, denied, invoiced, expired. Requires the quotes:all
    scope.

    Args:
        params.id (str): Quote internal ID.
        params.status (QuoteStatus): The new status.

    Returns:
        str: JSON of the updated quote, or an error string.
    """
    try:
        data = await _request("PUT", f"quotes/{params.id}/update_status", params.company,
                              json_body={"status": params.status.value})
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class SendQuoteEmailInput(_Base):
    """Send a quote by email."""
    id: str = Field(..., description="Quote internal ID.", min_length=1)
    company: Optional[str] = Field(default=None, description="Which configured company.")
    recipients: Optional[list[str]] = Field(
        default=None,
        description="Email addresses to send to. If omitted, Pennylane uses the "
                    "customer's email(s).",
    )


@mcp.tool(
    name="pennylane_send_quote_by_email",
    annotations={"title": "Send a quote by email", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_send_quote_by_email(params: SendQuoteEmailInput) -> str:
    """Email a quote to the customer (POST /quotes/{id}/send_by_email).

    The quote's PDF must be generated first — if you just created it, the API may
    briefly return 409 while the PDF renders; retry in a minute. Requires the
    quotes:all scope.

    Args:
        params.id (str): Quote internal ID.
        params.recipients (Optional[list[str]]): Override recipient emails.

    Returns:
        str: Confirmation that the email is on its way, or an error string.
    """
    try:
        body = {"recipients": params.recipients} if params.recipients else None
        data = await _request("POST", f"quotes/{params.id}/send_by_email",
                              params.company, json_body=body)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class InvoiceFromQuoteInput(_Base):
    """Create a customer invoice from an existing quote."""
    quote_id: int = Field(..., description="Internal ID of the quote to convert.")
    company: Optional[str] = Field(default=None, description="Which configured company.")
    draft: bool = Field(
        default=True,
        description="Create as a draft (True, default & safe). False finalizes it "
                    "immediately — assigns a legal number and locks it (irreversible).",
    )
    external_reference: Optional[str] = Field(
        default=None, description="Custom tracking reference (auto-generated if omitted)."
    )
    customer_invoice_template_id: Optional[int] = Field(
        default=None, description="Optional invoice template ID."
    )


@mcp.tool(
    name="pennylane_create_customer_invoice_from_quote",
    annotations={"title": "Create a customer invoice from a quote", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_create_customer_invoice_from_quote(params: InvoiceFromQuoteInput) -> str:
    """Generate a customer invoice from a quote (POST /customer_invoices/create_from_quote).

    The invoice inherits the customer and line items from the quote. Keep draft=True
    unless you intend to finalize immediately (draft=False is irreversible — confirm
    with the user first). Requires the customer_invoices:all scope.

    Args:
        params.quote_id (int): The source quote's internal ID.
        params.draft (bool): True = draft (default), False = finalize now.
        params.external_reference (Optional[str]): Custom reference.

    Returns:
        str: JSON of the created invoice (with its internal id), or an error string.
    """
    try:
        payload: dict[str, Any] = {"quote_id": params.quote_id, "draft": params.draft}
        if params.external_reference:
            payload["external_reference"] = params.external_reference
        if params.customer_invoice_template_id is not None:
            payload["customer_invoice_template_id"] = params.customer_invoice_template_id
        data = await _request("POST", "customer_invoices/create_from_quote",
                              params.company, json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


# --------------------------------------------------------------------------- #
# Banking: create/update transactions & reconciliation undo (unmatch)
# --------------------------------------------------------------------------- #

class CreateTransactionInput(_Base):
    """Create a banking transaction on a bank account."""
    bank_account_id: int = Field(
        ..., description="Internal ID of the bank account (list them via "
                         "pennylane_get with path 'bank_accounts')."
    )
    label: str = Field(..., description="Transaction label / description.", min_length=1)
    date: str = Field(..., description="Transaction date, YYYY-MM-DD.")
    amount: str = Field(
        ...,
        description="Amount as a STRING, e.g. \"120.00\". Use a leading minus for "
                    "a debit/outflow, e.g. \"-120.00\".",
    )
    company: Optional[str] = Field(default=None, description="Which configured company.")
    fee: Optional[str] = Field(default=None, description="Transaction fee as a STRING.")


@mcp.tool(
    name="pennylane_create_transaction",
    annotations={"title": "Create a bank transaction", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_create_transaction(params: CreateTransactionInput) -> str:
    """Create a banking transaction (POST /transactions). Requires the transactions:all scope.

    Amounts are STRINGS. `currency` is derived from the bank account and is not an
    input. Most transactions arrive automatically from the bank connection; use this
    only for manual/one-off entries.

    Args:
        params.bank_account_id (int), params.label (str), params.date (str),
        params.amount (str): Required (amount as a string).
        params.fee (Optional[str]): Fee as a string.

    Returns:
        str: JSON of the created transaction (with its internal id), or an error string.
    """
    try:
        payload: dict[str, Any] = {
            "bank_account_id": params.bank_account_id,
            "label": params.label,
            "date": params.date,
            "amount": params.amount,
        }
        if params.fee is not None:
            payload["fee"] = params.fee
        data = await _request("POST", "transactions", params.company, json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_update_transaction",
    annotations={"title": "Update a bank transaction", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_update_transaction(params: UpdateByIdInput) -> str:
    """Update a banking transaction (PUT /transactions/{id}).

    Common editable fields: label, date, amount (string). Requires the
    transactions:all scope.

    Args:
        params.id (str): Transaction internal ID.
        params.fields (dict): Fields to change (amounts as strings).

    Returns:
        str: JSON of the updated transaction, or an error string.
    """
    try:
        data = await _request("PUT", f"transactions/{params.id}", params.company,
                              json_body=params.fields)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class UnmatchTransactionInput(_Base):
    """Remove a transaction↔invoice reconciliation link."""
    invoice_id: int = Field(..., description="Internal ID of the invoice.")
    matched_transaction_id: int = Field(
        ...,
        description="ID of the matched_transaction RECORD to remove — read it from "
                    "the invoice's matched_transactions list (pennylane_get path "
                    "'<customer|supplier>_invoices/{id}/matched_transactions'). This "
                    "is NOT the raw bank transaction id.",
    )
    company: Optional[str] = Field(default=None, description="Which configured company.")


@mcp.tool(
    name="pennylane_unmatch_customer_invoice_transaction",
    annotations={"title": "Unmatch a transaction from a customer invoice", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_unmatch_customer_invoice_transaction(params: UnmatchTransactionInput) -> str:
    """Undo a customer-invoice reconciliation
    (DELETE /customer_invoices/{invoice_id}/matched_transactions/{id}).

    Removes the link created by pennylane_match_customer_invoice_transaction. The
    `matched_transaction_id` is the match record's id, not the transaction id.
    Requires the customer_invoices:all scope.

    Args:
        params.invoice_id (int): The customer invoice's internal ID.
        params.matched_transaction_id (int): The matched_transaction record id.

    Returns:
        str: Confirmation (HTTP 204) or an error string.
    """
    try:
        data = await _request(
            "DELETE",
            f"customer_invoices/{params.invoice_id}/matched_transactions/{params.matched_transaction_id}",
            params.company,
        )
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_unmatch_supplier_invoice_transaction",
    annotations={"title": "Unmatch a transaction from a supplier invoice", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_unmatch_supplier_invoice_transaction(params: UnmatchTransactionInput) -> str:
    """Undo a supplier-invoice reconciliation
    (DELETE /supplier_invoices/{invoice_id}/matched_transactions/{id}).

    Removes the link created by pennylane_match_supplier_invoice_transaction. The
    `matched_transaction_id` is the match record's id, not the transaction id.
    Requires the supplier_invoices:all scope.

    Args:
        params.invoice_id (int): The supplier invoice's internal ID.
        params.matched_transaction_id (int): The matched_transaction record id.

    Returns:
        str: Confirmation (HTTP 204) or an error string.
    """
    try:
        data = await _request(
            "DELETE",
            f"supplier_invoices/{params.invoice_id}/matched_transactions/{params.matched_transaction_id}",
            params.company,
        )
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


# --------------------------------------------------------------------------- #
# Analytical accounting: categorization & categories
# --------------------------------------------------------------------------- #

class CategorizeInput(_Base):
    """Assign analytical categories to a resource (REPLACES existing ones)."""
    id: str = Field(..., description="Internal ID of the resource to categorize.", min_length=1)
    categories: list[dict[str, Any]] = Field(
        ...,
        description='Array of {"id": <category_id int>, "weight": "<0..1 string>"}. '
                    'Weights within one category group must sum to 1 (e.g. a single '
                    'full split: [{"id":42,"weight":"1"}]). Pass [] to clear all. '
                    "This REPLACES the resource's current categories. Find category "
                    "ids via pennylane_get path 'categories'.",
    )
    company: Optional[str] = Field(default=None, description="Which configured company.")


async def _categorize(resource: str, params: CategorizeInput) -> str:
    """Shared helper: PUT the bare category array to /{resource}/{id}/categories."""
    try:
        data = await _request("PUT", f"{resource}/{params.id}/categories",
                              params.company, json_body=params.categories)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_categorize_customer_invoice",
    annotations={"title": "Categorize a customer invoice", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_categorize_customer_invoice(params: CategorizeInput) -> str:
    """Set analytical categories on a customer invoice (PUT /customer_invoices/{id}/categories).

    Replaces existing categories; weights per group must sum to 1. Requires the
    customer_invoices:all scope.
    """
    return await _categorize("customer_invoices", params)


@mcp.tool(
    name="pennylane_categorize_supplier_invoice",
    annotations={"title": "Categorize a supplier invoice", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_categorize_supplier_invoice(params: CategorizeInput) -> str:
    """Set analytical categories on a supplier invoice (PUT /supplier_invoices/{id}/categories).

    Replaces existing categories; weights per group must sum to 1. Requires the
    supplier_invoices:all scope.
    """
    return await _categorize("supplier_invoices", params)


@mcp.tool(
    name="pennylane_categorize_customer",
    annotations={"title": "Categorize a customer", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_categorize_customer(params: CategorizeInput) -> str:
    """Set analytical categories on a customer (PUT /customers/{id}/categories).

    Replaces existing categories; weights per group must sum to 1. Requires the
    customers:all scope.
    """
    return await _categorize("customers", params)


@mcp.tool(
    name="pennylane_categorize_supplier",
    annotations={"title": "Categorize a supplier", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_categorize_supplier(params: CategorizeInput) -> str:
    """Set analytical categories on a supplier (PUT /suppliers/{id}/categories).

    Replaces existing categories; weights per group must sum to 1. Requires the
    suppliers:all scope.
    """
    return await _categorize("suppliers", params)


@mcp.tool(
    name="pennylane_categorize_transaction",
    annotations={"title": "Categorize a bank transaction", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_categorize_transaction(params: CategorizeInput) -> str:
    """Set analytical categories on a bank transaction (PUT /transactions/{id}/categories).

    Replaces existing categories; weights per group must sum to 1. Requires the
    transactions:all scope.
    """
    return await _categorize("transactions", params)


@mcp.tool(
    name="pennylane_categorize_ledger_entry_line",
    annotations={"title": "Categorize a ledger entry line", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_categorize_ledger_entry_line(params: CategorizeInput) -> str:
    """Link analytical categories to a ledger entry line
    (PUT /ledger_entry_lines/{id}/categories).

    Replaces existing categories; weights per group must sum to 1. Requires the
    ledger scope.
    """
    return await _categorize("ledger_entry_lines", params)


class CreateCategoryInput(_Base):
    """Create an analytical category."""
    label: str = Field(..., description="Category name, e.g. 'Marketing'.", min_length=1)
    company: Optional[str] = Field(default=None, description="Which configured company.")
    category_group_id: Optional[int] = Field(
        default=None,
        description="ID of the category group this belongs to (list via pennylane_get "
                    "path 'category_groups'). Usually required.",
    )
    analytical_code: Optional[str] = Field(default=None, description="Optional analytical code.")
    direction: Optional[str] = Field(
        default=None,
        description="Only for treasury categories: 'cash_in' or 'cash_out'.",
    )
    extra_fields: Optional[dict[str, Any]] = Field(
        default=None, description="Any other category fields passed straight through."
    )


@mcp.tool(
    name="pennylane_create_category",
    annotations={"title": "Create an analytical category", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_create_category(params: CreateCategoryInput) -> str:
    """Create an analytical category (POST /categories).

    Categories belong to a category group — pass category_group_id (list groups via
    pennylane_get path 'category_groups').

    Args:
        params.label (str): Required category name.
        params.category_group_id (Optional[int]): Owning group id.
        params.analytical_code / direction (Optional): Optional attributes.

    Returns:
        str: JSON of the created category (with its internal id), or an error string.
    """
    try:
        payload: dict[str, Any] = {"label": params.label}
        if params.category_group_id is not None:
            payload["category_group_id"] = params.category_group_id
        if params.analytical_code is not None:
            payload["analytical_code"] = params.analytical_code
        if params.direction is not None:
            payload["direction"] = params.direction
        if params.extra_fields:
            payload.update(params.extra_fields)
        data = await _request("POST", "categories", params.company, json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_update_category",
    annotations={"title": "Update an analytical category", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_update_category(params: UpdateByIdInput) -> str:
    """Update an analytical category (PUT /categories/{id}).

    Editable fields: label, analytical_code, direction ('cash_in'/'cash_out', for
    treasury categories only). The category group cannot be changed here.

    Args:
        params.id (str): Category internal ID.
        params.fields (dict): Fields to change.

    Returns:
        str: JSON of the updated category, or an error string.
    """
    try:
        data = await _request("PUT", f"categories/{params.id}", params.company,
                              json_body=params.fields)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


# --------------------------------------------------------------------------- #
# Accounting structure: journals, ledger accounts, ledger entries, lettering
# --------------------------------------------------------------------------- #

class CreateJournalInput(_Base):
    """Create an accounting journal."""
    label: str = Field(..., description="Journal name, e.g. 'Sales'.", min_length=1)
    company: Optional[str] = Field(default=None, description="Which configured company.")
    code: Optional[str] = Field(default=None, description="Short journal code, e.g. 'VE'.")
    extra_fields: Optional[dict[str, Any]] = Field(
        default=None, description="Any other journal fields passed straight through."
    )


@mcp.tool(
    name="pennylane_create_journal",
    annotations={"title": "Create an accounting journal", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_create_journal(params: CreateJournalInput) -> str:
    """Create an accounting journal (POST /journals). Requires the ledger scope.

    Returns:
        str: JSON of the created journal (with its internal id), or an error string.
    """
    try:
        payload: dict[str, Any] = {"label": params.label}
        if params.code:
            payload["code"] = params.code
        if params.extra_fields:
            payload.update(params.extra_fields)
        data = await _request("POST", "journals", params.company, json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class CreateLedgerAccountInput(_Base):
    """Create a ledger account (chart of accounts entry)."""
    number: str = Field(..., description="Account number, e.g. '706000'.", min_length=1)
    label: str = Field(..., description="Account label.", min_length=1)
    company: Optional[str] = Field(default=None, description="Which configured company.")
    extra_fields: Optional[dict[str, Any]] = Field(
        default=None, description="Any other account fields passed straight through."
    )


@mcp.tool(
    name="pennylane_create_ledger_account",
    annotations={"title": "Create a ledger account", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_create_ledger_account(params: CreateLedgerAccountInput) -> str:
    """Create a ledger account / plan comptable entry (POST /ledger_accounts).

    Requires the ledger scope.

    Returns:
        str: JSON of the created ledger account (with its internal id), or an error.
    """
    try:
        payload: dict[str, Any] = {"number": params.number, "label": params.label}
        if params.extra_fields:
            payload.update(params.extra_fields)
        data = await _request("POST", "ledger_accounts", params.company, json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_update_ledger_account",
    annotations={"title": "Update a ledger account", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_update_ledger_account(params: UpdateByIdInput) -> str:
    """Update a ledger account (PATCH /ledger_accounts/{id}).

    Common fields: label, number. Requires the ledger scope.

    Args:
        params.id (str): Ledger account internal ID.
        params.fields (dict): Fields to change.

    Returns:
        str: JSON of the updated ledger account, or an error string.
    """
    try:
        data = await _request("PATCH", f"ledger_accounts/{params.id}", params.company,
                              json_body=params.fields)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class CreateLedgerEntryInput(_Base):
    """Create a ledger (journal) entry with balanced double-entry lines."""
    date: str = Field(..., description="Entry date, YYYY-MM-DD.")
    journal_id: int = Field(..., description="Journal internal ID (see pennylane_list_journals).")
    ledger_entry_lines: list[dict[str, Any]] = Field(
        ...,
        description='Double-entry lines (>=2). Each: {"ledger_account_id":int,'
                    '"label":str,"debit":"<string>","credit":"<string>"}. Amounts '
                    'are STRINGS; total debits MUST equal total credits. Put "0" on '
                    "the unused side of each line.",
        min_length=2,
    )
    company: Optional[str] = Field(default=None, description="Which configured company.")
    label: Optional[str] = Field(default=None, description="Entry label / description.")
    extra_fields: Optional[dict[str, Any]] = Field(
        default=None, description="Any other entry fields passed straight through."
    )


@mcp.tool(
    name="pennylane_create_ledger_entry",
    annotations={"title": "Create a ledger entry", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_create_ledger_entry(params: CreateLedgerEntryInput) -> str:
    """Create a ledger entry — a balanced journal entry (POST /ledger_entries).

    Amounts are STRINGS and total debits must equal total credits, or the API
    returns 422. Reference accounts by ledger_account_id (not account number).
    Requires the ledger scope.

    Args:
        params.date (str), params.journal_id (int): Required.
        params.ledger_entry_lines (list): Balanced debit/credit lines (strings).
        params.label (Optional[str]): Entry description.

    Returns:
        str: JSON of the created ledger entry (with its internal id), or an error.
    """
    try:
        payload: dict[str, Any] = {
            "date": params.date,
            "journal_id": params.journal_id,
            "ledger_entry_lines": params.ledger_entry_lines,
        }
        if params.label:
            payload["label"] = params.label
        if params.extra_fields:
            payload.update(params.extra_fields)
        data = await _request("POST", "ledger_entries", params.company, json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_update_ledger_entry",
    annotations={"title": "Update a ledger entry", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_update_ledger_entry(params: UpdateByIdInput) -> str:
    """Update a ledger entry (PUT /ledger_entries/{id}).

    Common fields: date, label, ledger_entry_lines (amounts as strings; debits must
    equal credits). Requires the ledger scope.

    Args:
        params.id (str): Ledger entry internal ID.
        params.fields (dict): Fields to change (amounts as strings).

    Returns:
        str: JSON of the updated ledger entry, or an error string.
    """
    try:
        data = await _request("PUT", f"ledger_entries/{params.id}", params.company,
                              json_body=params.fields)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class LetteringInput(_Base):
    """Letter or unletter ledger entry lines together."""
    line_ids: list[int] = Field(
        ...,
        description="IDs of the ledger entry lines to (un)letter. Lettering needs "
                    "at least 2 lines; unlettering at least 1.",
        min_length=1,
    )
    company: Optional[str] = Field(default=None, description="Which configured company.")
    unbalanced_lettering_strategy: str = Field(
        default="none",
        description="'none' (default; reject if the lettered lines don't balance) "
                    "or 'partial' (allow partial lettering).",
    )


def _lettering_body(params: LetteringInput) -> dict[str, Any]:
    return {
        "unbalanced_lettering_strategy": params.unbalanced_lettering_strategy,
        "ledger_entry_lines": [{"id": i} for i in params.line_ids],
    }


@mcp.tool(
    name="pennylane_letter_ledger_entry_lines",
    annotations={"title": "Letter ledger entry lines", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_letter_ledger_entry_lines(params: LetteringInput) -> str:
    """Letter ledger entry lines together (POST /ledger_entry_lines/lettering).

    Pass at least 2 line ids. Reconciles the lines (e.g. an invoice against its
    payment). Requires the ledger scope.

    Args:
        params.line_ids (list[int]): Line ids to letter together (>=2).

    Returns:
        str: Confirmation or an error string.
    """
    try:
        data = await _request("POST", "ledger_entry_lines/lettering", params.company,
                              json_body=_lettering_body(params))
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_unletter_ledger_entry_lines",
    annotations={"title": "Unletter ledger entry lines", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_unletter_ledger_entry_lines(params: LetteringInput) -> str:
    """Unletter ledger entry lines (DELETE /ledger_entry_lines/lettering).

    Removes an existing lettering/reconciliation link. Requires the ledger scope.

    Args:
        params.line_ids (list[int]): Line ids to unletter (>=1).

    Returns:
        str: Confirmation or an error string.
    """
    try:
        data = await _request("DELETE", "ledger_entry_lines/lettering", params.company,
                              json_body=_lettering_body(params))
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


# --------------------------------------------------------------------------- #
# Invoice status, e-invoicing & document links
# --------------------------------------------------------------------------- #

class ActionInput(_Base):
    """Trigger an action on a resource, with an optional request body."""
    id: str = Field(..., description="Internal ID of the target resource.", min_length=1)
    company: Optional[str] = Field(default=None, description="Which configured company.")
    body: Optional[dict[str, Any]] = Field(
        default=None, description="Optional request body fields for the action."
    )


class SupplierPaymentStatus(str, Enum):
    """Allowed supplier-invoice payment statuses."""
    PAID = "paid"
    TO_BE_PAID = "to_be_paid"


class SupplierPaymentStatusInput(_Base):
    """Set a supplier invoice's payment status."""
    id: str = Field(..., description="Supplier invoice internal ID.", min_length=1)
    payment_status: SupplierPaymentStatus = Field(
        ..., description="New payment status: 'paid' or 'to_be_paid'."
    )
    company: Optional[str] = Field(default=None, description="Which configured company.")


@mcp.tool(
    name="pennylane_update_supplier_invoice_payment_status",
    annotations={"title": "Set a supplier invoice's payment status", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_update_supplier_invoice_payment_status(params: SupplierPaymentStatusInput) -> str:
    """Set a supplier invoice's payment status (PUT /supplier_invoices/{id}/payment_status).

    Values: 'paid' or 'to_be_paid'. This flags the status only; it does not reconcile
    with a bank transaction. Requires the supplier_invoices:all scope.

    Args:
        params.id (str): Supplier invoice internal ID.
        params.payment_status (SupplierPaymentStatus): paid / to_be_paid.

    Returns:
        str: JSON confirmation or an error string.
    """
    try:
        data = await _request("PUT", f"supplier_invoices/{params.id}/payment_status",
                              params.company, json_body={"payment_status": params.payment_status.value})
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_update_supplier_invoice_e_invoice_status",
    annotations={"title": "Set a supplier invoice's e-invoice status", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_update_supplier_invoice_e_invoice_status(params: ActionInput) -> str:
    """Set a supplier invoice's e-invoice status (PUT /supplier_invoices/{id}/e_invoice_status).

    Pass the status in `body`, e.g. {"e_invoice_status": "<value>"} (see the Pennylane
    e-invoicing docs for the allowed values in your setup). Requires the
    supplier_invoices:all scope.

    Args:
        params.id (str): Supplier invoice internal ID.
        params.body (dict): e.g. {"e_invoice_status": "..."}.

    Returns:
        str: JSON confirmation or an error string.
    """
    try:
        data = await _request("PUT", f"supplier_invoices/{params.id}/e_invoice_status",
                              params.company, json_body=params.body)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_validate_supplier_invoice_accounting",
    annotations={"title": "Validate a supplier invoice's accounting", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_validate_supplier_invoice_accounting(params: ActionInput) -> str:
    """Validate a supplier invoice's accounting (POST /supplier_invoices/{id}/validate_accounting).

    Marks the invoice's accounting as validated. Any options go in `body` (usually
    none needed). Requires the supplier_invoices:all scope.

    Args:
        params.id (str): Supplier invoice internal ID.

    Returns:
        str: JSON confirmation or an error string.
    """
    try:
        data = await _request("POST", f"supplier_invoices/{params.id}/validate_accounting",
                              params.company, json_body=params.body)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_import_supplier_e_invoice",
    annotations={"title": "Import a supplier e-invoice", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_import_supplier_e_invoice(params: ActionInput) -> str:
    """Import an e-invoice onto a supplier invoice (POST /supplier_invoices/{id}/import_e_invoice).

    E-invoice import is a BETA endpoint and may change. Pass the required payload in
    `body` per the Pennylane e-invoicing docs. Requires the supplier_invoices:all scope.

    Args:
        params.id (str): Supplier invoice internal ID.
        params.body (dict): The e-invoice import payload.

    Returns:
        str: JSON confirmation or an error string.
    """
    try:
        data = await _request("POST", f"supplier_invoices/{params.id}/import_e_invoice",
                              params.company, json_body=params.body)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_send_customer_invoice_to_pa",
    annotations={"title": "Send a customer invoice to public administration", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_send_customer_invoice_to_pa(params: ActionInput) -> str:
    """Send a finalized customer invoice to the public administration / e-invoicing
    network (POST /customer_invoices/{id}/send_to_pa).

    For B2G / e-invoicing (e.g. Chorus Pro). The invoice must be finalized. Any
    options go in `body` (usually none). Requires the customer_invoices:all scope.

    Args:
        params.id (str): Customer invoice internal ID.

    Returns:
        str: JSON confirmation or an error string.
    """
    try:
        data = await _request("POST", f"customer_invoices/{params.id}/send_to_pa",
                              params.company, json_body=params.body)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_import_customer_e_invoice",
    annotations={"title": "Import a customer e-invoice", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_import_customer_e_invoice(params: ActionInput) -> str:
    """Import an e-invoice onto a customer invoice (POST /customer_invoices/{id}/import_e_invoice).

    E-invoice import is a BETA endpoint and may change. Pass the required payload in
    `body` per the Pennylane e-invoicing docs. Requires the customer_invoices:all scope.

    Args:
        params.id (str): Customer invoice internal ID.
        params.body (dict): The e-invoice import payload.

    Returns:
        str: JSON confirmation or an error string.
    """
    try:
        data = await _request("POST", f"customer_invoices/{params.id}/import_e_invoice",
                              params.company, json_body=params.body)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class LinkCreditNoteInput(_Base):
    """Link a credit note to a customer invoice."""
    id: str = Field(..., description="Customer invoice internal ID.", min_length=1)
    credit_note_id: int = Field(..., description="Internal ID of the credit note to link.")
    company: Optional[str] = Field(default=None, description="Which configured company.")


@mcp.tool(
    name="pennylane_link_credit_note",
    annotations={"title": "Link a credit note to a customer invoice", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_link_credit_note(params: LinkCreditNoteInput) -> str:
    """Link a credit note to a customer invoice (POST /customer_invoices/{id}/link_credit_note).

    Requires the customer_invoices:all scope.

    Args:
        params.id (str): The customer invoice internal ID.
        params.credit_note_id (int): The credit note's internal ID.

    Returns:
        str: JSON confirmation or an error string.
    """
    try:
        data = await _request("POST", f"customer_invoices/{params.id}/link_credit_note",
                              params.company, json_body={"credit_note_id": params.credit_note_id})
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class LinkPurchaseRequestInput(_Base):
    """Link a purchase request to a supplier invoice."""
    id: str = Field(..., description="Supplier invoice internal ID.", min_length=1)
    purchase_request_id: int = Field(..., description="Internal ID of the purchase request to link.")
    company: Optional[str] = Field(default=None, description="Which configured company.")
    extra_fields: Optional[dict[str, Any]] = Field(
        default=None, description="Any other fields passed straight through."
    )


@mcp.tool(
    name="pennylane_link_purchase_request_to_supplier_invoice",
    annotations={"title": "Link a purchase request to a supplier invoice", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_link_purchase_request_to_supplier_invoice(params: LinkPurchaseRequestInput) -> str:
    """Link a purchase request to a supplier invoice
    (POST /supplier_invoices/{id}/linked_purchase_requests).

    Requires the supplier_invoices:all scope.

    Args:
        params.id (str): The supplier invoice internal ID.
        params.purchase_request_id (int): The purchase request's internal ID.

    Returns:
        str: JSON confirmation or an error string.
    """
    try:
        payload: dict[str, Any] = {"purchase_request_id": params.purchase_request_id}
        if params.extra_fields:
            payload.update(params.extra_fields)
        data = await _request("POST", f"supplier_invoices/{params.id}/linked_purchase_requests",
                              params.company, json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


# --------------------------------------------------------------------------- #
# Imports, banking, subscriptions & appendices
# --------------------------------------------------------------------------- #

class ImportCustomerInvoiceInput(_Base):
    """Import a customer invoice from an already-uploaded PDF."""
    file_attachment_id: int = Field(..., description="ID from pennylane_upload_file_attachment.")
    date: str = Field(..., description="Invoice date, YYYY-MM-DD.")
    customer_id: int = Field(..., description="Internal ID of the customer.")
    company: Optional[str] = Field(default=None, description="Which configured company.")
    deadline: Optional[str] = Field(default=None, description="Payment due date, YYYY-MM-DD (required by the API).")
    currency_amount: Optional[str] = Field(default=None, description="Total incl. tax, STRING e.g. \"120.00\".")
    currency_amount_before_tax: Optional[str] = Field(default=None, description="Total excl. tax, STRING.")
    currency_tax: Optional[str] = Field(default=None, description="Total VAT, STRING.")
    invoice_lines: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description="Line items (>=1). Each: {currency_amount, currency_tax, quantity, "
                    "raw_currency_unit_price, unit, vat_rate} as strings where monetary.",
    )
    extra_fields: Optional[dict[str, Any]] = Field(
        default=None, description="Any other import fields (invoice_number, external_reference, ...)."
    )


@mcp.tool(
    name="pennylane_import_customer_invoice",
    annotations={"title": "Import a customer invoice (from uploaded PDF)", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_import_customer_invoice(params: ImportCustomerInvoiceInput) -> str:
    """Import a customer invoice from a previously uploaded PDF (POST /customer_invoices/import).

    Workflow: pennylane_upload_file_attachment -> use its id here. Amounts are STRINGS;
    line totals must reconcile with the invoice totals or the API returns 422. The API
    also requires date, deadline, customer_id, the three currency_amount* totals and at
    least one invoice_line. Requires the customer_invoices:all scope.

    Args:
        params.file_attachment_id (int), params.date (str), params.customer_id (int): Required.
        params.deadline / currency_amount* / invoice_lines: Also required by the API.

    Returns:
        str: JSON of the imported customer invoice (with its internal id), or an error.
    """
    try:
        payload: dict[str, Any] = {
            "file_attachment_id": params.file_attachment_id,
            "date": params.date,
            "customer_id": params.customer_id,
        }
        for field in ("deadline", "currency_amount", "currency_amount_before_tax",
                      "currency_tax", "invoice_lines"):
            value = getattr(params, field)
            if value is not None:
                payload[field] = value
        if params.extra_fields:
            payload.update(params.extra_fields)
        data = await _request("POST", "customer_invoices/import", params.company, json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class ImportPurchaseRequestInput(_Base):
    """Import a purchase request / order from an uploaded PDF."""
    file_attachment_id: int = Field(..., description="ID from pennylane_upload_file_attachment.")
    company: Optional[str] = Field(default=None, description="Which configured company.")
    date: Optional[str] = Field(default=None, description="Document date, YYYY-MM-DD.")
    supplier_id: Optional[int] = Field(default=None, description="Internal ID of the supplier.")
    extra_fields: Optional[dict[str, Any]] = Field(
        default=None, description="Any other import fields passed straight through (amounts as strings)."
    )


@mcp.tool(
    name="pennylane_import_purchase_request",
    annotations={"title": "Import a purchase request (from uploaded PDF)", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_import_purchase_request(params: ImportPurchaseRequestInput) -> str:
    """Import a purchase request / order from a previously uploaded PDF
    (POST /purchase_requests/import).

    Workflow: pennylane_upload_file_attachment -> use its id here. Pass amounts as
    strings via extra_fields. Requires the relevant purchase scope.

    Args:
        params.file_attachment_id (int): Uploaded PDF's attachment id.
        params.date / supplier_id (Optional): Common fields.
        params.extra_fields (Optional[dict]): Other import fields.

    Returns:
        str: JSON of the imported purchase request, or an error string.
    """
    try:
        payload: dict[str, Any] = {"file_attachment_id": params.file_attachment_id}
        if params.date:
            payload["date"] = params.date
        if params.supplier_id is not None:
            payload["supplier_id"] = params.supplier_id
        if params.extra_fields:
            payload.update(params.extra_fields)
        data = await _request("POST", "purchase_requests/import", params.company, json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class CreateWithFieldsInput(_Base):
    """Create a resource by passing the full payload as an object."""
    company: Optional[str] = Field(default=None, description="Which configured company.")
    fields: dict[str, Any] = Field(
        ..., description="The full create payload as an object. Monetary amounts as strings."
    )


@mcp.tool(
    name="pennylane_create_bank_account",
    annotations={"title": "Create a bank account", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_create_bank_account(params: CreateWithFieldsInput) -> str:
    """Create a bank account (POST /bank_accounts). Requires the bank_accounts:all scope.

    Most bank accounts arrive from the bank connection; use this for manual entries.
    Pass the payload in `fields` (commonly name/label, iban, currency).

    Args:
        params.fields (dict): The bank account payload.

    Returns:
        str: JSON of the created bank account (with its internal id), or an error string.
    """
    try:
        data = await _request("POST", "bank_accounts", params.company, json_body=params.fields)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_create_billing_subscription",
    annotations={"title": "Create a billing subscription", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_create_billing_subscription(params: CreateWithFieldsInput) -> str:
    """Create a recurring billing subscription (POST /billing_subscriptions).

    Pass the full payload in `fields` (commonly customer_id, start_date, frequency/
    recurring settings, and invoice_lines with string amounts). Requires the
    customer_invoices:all scope.

    Args:
        params.fields (dict): The subscription payload (amounts as strings).

    Returns:
        str: JSON of the created subscription (with its internal id), or an error string.
    """
    try:
        data = await _request("POST", "billing_subscriptions", params.company, json_body=params.fields)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_update_billing_subscription",
    annotations={"title": "Update a billing subscription", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_update_billing_subscription(params: UpdateByIdInput) -> str:
    """Update a recurring billing subscription (PUT /billing_subscriptions/{id}).

    Common fields: start_date, frequency/recurring settings, invoice_lines (string
    amounts). Requires the customer_invoices:all scope.

    Args:
        params.id (str): Subscription internal ID.
        params.fields (dict): Fields to change (amounts as strings).

    Returns:
        str: JSON of the updated subscription, or an error string.
    """
    try:
        data = await _request("PUT", f"billing_subscriptions/{params.id}", params.company,
                              json_body=params.fields)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class UploadAppendixInput(_Base):
    """Upload a local PDF as an appendix to a document."""
    id: str = Field(..., description="Internal ID of the invoice / quote / document.", min_length=1)
    file_path: str = Field(..., description="Absolute path to a local PDF file.", min_length=1)
    company: Optional[str] = Field(default=None, description="Which configured company.")


async def _upload_appendix(resource: str, params: UploadAppendixInput) -> str:
    """Shared helper: multipart-upload a PDF to /{resource}/{id}/appendices."""
    try:
        p = Path(params.file_path).expanduser()
        if not p.is_file():
            return f"Error: no file found at '{params.file_path}'. Provide an absolute path to a PDF."
        files = {"file": (p.name, p.read_bytes(), "application/pdf")}
        data = await _request("POST", f"{resource}/{params.id}/appendices", params.company, files=files)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_upload_customer_invoice_appendix",
    annotations={"title": "Upload a customer-invoice appendix", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_upload_customer_invoice_appendix(params: UploadAppendixInput) -> str:
    """Attach a PDF appendix to a customer invoice
    (multipart POST /customer_invoices/{id}/appendices). Requires the customer_invoices:all scope.

    Args:
        params.id (str): Customer invoice internal ID.
        params.file_path (str): Absolute path to a local PDF.

    Returns:
        str: JSON of the uploaded appendix, or an error string.
    """
    return await _upload_appendix("customer_invoices", params)


@mcp.tool(
    name="pennylane_upload_quote_appendix",
    annotations={"title": "Upload a quote appendix", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_upload_quote_appendix(params: UploadAppendixInput) -> str:
    """Attach a PDF appendix to a quote (multipart POST /quotes/{id}/appendices).
    Requires the quotes:all scope.

    Args:
        params.id (str): Quote internal ID.
        params.file_path (str): Absolute path to a local PDF.

    Returns:
        str: JSON of the uploaded appendix, or an error string.
    """
    return await _upload_appendix("quotes", params)


@mcp.tool(
    name="pennylane_upload_commercial_document_appendix",
    annotations={"title": "Upload a commercial-document appendix", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_upload_commercial_document_appendix(params: UploadAppendixInput) -> str:
    """Attach a PDF appendix to a commercial document
    (multipart POST /commercial_documents/{id}/appendices).

    Args:
        params.id (str): Commercial document internal ID.
        params.file_path (str): Absolute path to a local PDF.

    Returns:
        str: JSON of the uploaded appendix, or an error string.
    """
    return await _upload_appendix("commercial_documents", params)


# --------------------------------------------------------------------------- #
# Direct-debit mandates: SEPA / GoCardless / Pro Account (niche)
# --------------------------------------------------------------------------- #

class BodyOnlyInput(_Base):
    """A collection-level action with an optional request body."""
    company: Optional[str] = Field(default=None, description="Which configured company.")
    body: Optional[dict[str, Any]] = Field(
        default=None, description="Optional request body fields for the action."
    )


@mcp.tool(
    name="pennylane_create_sepa_mandate",
    annotations={"title": "Create a SEPA mandate", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_create_sepa_mandate(params: CreateWithFieldsInput) -> str:
    """Create a SEPA direct-debit mandate (POST /sepa_mandates).

    Pass the mandate payload in `fields` (commonly customer_id, iban, rum/reference,
    signature date) per the Pennylane SEPA docs.

    Args:
        params.fields (dict): The mandate payload.

    Returns:
        str: JSON of the created mandate (with its internal id), or an error string.
    """
    try:
        data = await _request("POST", "sepa_mandates", params.company, json_body=params.fields)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_update_sepa_mandate",
    annotations={"title": "Update a SEPA mandate", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_update_sepa_mandate(params: UpdateByIdInput) -> str:
    """Update a SEPA direct-debit mandate (PUT /sepa_mandates/{id}).

    Args:
        params.id (str): SEPA mandate internal ID.
        params.fields (dict): Fields to change.

    Returns:
        str: JSON of the updated mandate, or an error string.
    """
    try:
        data = await _request("PUT", f"sepa_mandates/{params.id}", params.company,
                              json_body=params.fields)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_delete_sepa_mandate",
    annotations={"title": "Delete a SEPA mandate", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_delete_sepa_mandate(params: GetByIdInput) -> str:
    """Delete a SEPA direct-debit mandate (DELETE /sepa_mandates/{id}).

    Irreversible; confirm with the user first.

    Args:
        params.id (str): SEPA mandate internal ID.

    Returns:
        str: Confirmation (HTTP 204) or an error string.
    """
    try:
        data = await _request("DELETE", f"sepa_mandates/{params.id}", params.company)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_associate_gocardless_mandate",
    annotations={"title": "Associate a GoCardless mandate", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_associate_gocardless_mandate(params: CreateWithFieldsInput) -> str:
    """Associate a GoCardless mandate (POST /gocardless_mandates/associations).

    Pass the association payload in `fields` (commonly customer_id and the GoCardless
    mandate reference) per the Pennylane GoCardless docs.

    Args:
        params.fields (dict): The association payload.

    Returns:
        str: JSON confirmation, or an error string.
    """
    try:
        data = await _request("POST", "gocardless_mandates/associations", params.company,
                              json_body=params.fields)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_send_gocardless_mandate_mail_request",
    annotations={"title": "Send a GoCardless mandate email request", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_send_gocardless_mandate_mail_request(params: ActionInput) -> str:
    """Email a GoCardless mandate signature request to the customer
    (POST /gocardless_mandates/{id}/mail_requests).

    Args:
        params.id (str): GoCardless mandate internal ID.
        params.body (Optional[dict]): Optional request options.

    Returns:
        str: JSON confirmation, or an error string.
    """
    try:
        data = await _request("POST", f"gocardless_mandates/{params.id}/mail_requests",
                              params.company, json_body=params.body)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_cancel_gocardless_mandate",
    annotations={"title": "Cancel a GoCardless mandate", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_cancel_gocardless_mandate(params: ActionInput) -> str:
    """Cancel a GoCardless mandate (POST /gocardless_mandates/{id}/cancellations).

    Irreversible; confirm with the user first.

    Args:
        params.id (str): GoCardless mandate internal ID.
        params.body (Optional[dict]): Optional cancellation options.

    Returns:
        str: JSON confirmation, or an error string.
    """
    try:
        data = await _request("POST", f"gocardless_mandates/{params.id}/cancellations",
                              params.company, json_body=params.body)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_migrate_pro_account_mandate",
    annotations={"title": "Migrate a mandate to Pro Account", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_migrate_pro_account_mandate(params: CreateWithFieldsInput) -> str:
    """Migrate a mandate to a Pennylane Pro Account (POST /pro_account_mandates/migrations).

    Pass the migration payload in `fields` (see pro_account_mandates/migration_candidates
    via pennylane_get for eligible mandates).

    Args:
        params.fields (dict): The migration payload.

    Returns:
        str: JSON confirmation, or an error string.
    """
    try:
        data = await _request("POST", "pro_account_mandates/migrations", params.company,
                              json_body=params.fields)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_send_pro_account_mandate_mail_request",
    annotations={"title": "Send a Pro Account mandate email request", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_send_pro_account_mandate_mail_request(params: BodyOnlyInput) -> str:
    """Email a Pro Account mandate request to a customer
    (POST /pro_account_mandates/mail_requests).

    Pass any required options (e.g. customer_id) in `body` per the Pennylane docs.

    Args:
        params.body (Optional[dict]): Request payload (e.g. {"customer_id": 123}).

    Returns:
        str: JSON confirmation, or an error string.
    """
    try:
        data = await _request("POST", "pro_account_mandates/mail_requests", params.company,
                              json_body=params.body)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


# =========================================================================== #
# Firm API (cabinet / accounting-firm token) — /api/external/firm/v1
# =========================================================================== #
# A SEPARATE API from Company v2: firm token, own base URL, the client company
# (dossier) selected via /companies/{company_id}/ in the path, own scope names
# (journals:all, ledger_entries:all, companies:readonly, dms_files:all, ...).
# Company-scoped lists paginate with cursor/limit like v2; the firm-level
# /companies list and the trial balance paginate with page/per_page.

class FirmListCompaniesInput(_Base):
    """List the accounting firm's client companies (page-based pagination)."""
    page: Optional[int] = Field(
        default=None, ge=1, description="Page number, starting at 1 (default 1)."
    )
    per_page: Optional[int] = Field(
        default=None, ge=1, le=100, description="Items per page (default 20)."
    )
    filters: Optional[list[dict[str, Any]]] = Field(
        default=None, description="Optional filter array ({field,operator,value})."
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)


class FirmGetCompanyInput(_Base):
    company_id: int = Field(..., description="Pennylane company ID (see "
                                              "pennylane_firm_list_companies).")
    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)


class FirmListInput(_Base):
    """Common inputs for company-scoped firm listing endpoints (cursor pagination)."""
    company_id: Optional[int] = Field(
        default=None,
        description="Pennylane company ID of the client company (dossier) to "
                    "query — from pennylane_firm_list_companies. Omit to use "
                    "PENNYLANE_FIRM_DEFAULT_COMPANY_ID if configured.",
    )
    filters: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description="Pennylane filter array ({field,operator,value}), same "
                    "format as the v2 API.",
    )
    sort: Optional[str] = Field(
        default=None, description="Sort attribute, e.g. 'date' or '-date' (descending)."
    )
    cursor: Optional[str] = Field(
        default=None,
        description="Opaque pagination cursor from a previous response's next_cursor.",
    )
    limit: Optional[int] = Field(
        default=20, ge=1, le=1000, description="Max items to return (default 20)."
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)


def _firm_list(path_suffix: str):
    """Build the coroutine body shared by the company-scoped firm list tools."""
    async def run(params: FirmListInput) -> str:
        try:
            query = _build_list_params(params.filters, params.cursor, params.limit,
                                       {"sort": params.sort})
            data = await _firm_request(
                "GET", _firm_company_path(params.company_id, path_suffix), query)
            return _format(data, params.response_format)
        except PennylaneError as exc:
            return str(exc)
    return run


@mcp.tool(
    name="pennylane_firm_list_companies",
    annotations={"title": "Firm: list client companies", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_firm_list_companies(params: FirmListCompaniesInput) -> str:
    """List the client companies (dossiers) visible to the firm token.

    This is the entry point for all other pennylane_firm_* tools: use the
    returned numeric `id` as `company_id`. NOTE: unlike the other list tools,
    this endpoint paginates with page/per_page (page starts at 1), not with a
    cursor. Requires the companies:readonly firm scope.

    Args:
        params.page (Optional[int]): Page number, starting at 1.
        params.per_page (Optional[int]): Items per page (default 20).
        params.filters (Optional[list]): Filter array ({field,operator,value}).

    Returns:
        str: JSON list of companies (id, name, ...), or an error string.
    """
    try:
        query: dict[str, Any] = {}
        if params.page is not None:
            query["page"] = params.page
        if params.per_page is not None:
            query["per_page"] = params.per_page
        if params.filters:
            query["filter"] = json.dumps(params.filters)
        data = await _firm_request("GET", "companies", query or None)
        return _format(data, params.response_format)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_firm_get_company",
    annotations={"title": "Firm: get one client company", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_firm_get_company(params: FirmGetCompanyInput) -> str:
    """Get the details of one client company of the firm (GET /companies/{id}).

    Requires the companies:readonly firm scope.

    Returns:
        str: JSON of the company, or an error string.
    """
    try:
        data = await _firm_request("GET", f"companies/{params.company_id}")
        return _format(data, params.response_format)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_firm_list_customers",
    annotations={"title": "Firm: list a client company's customers", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_firm_list_customers(params: FirmListInput) -> str:
    """List a client company's customers via the Firm API.

    Requires the customers:readonly firm scope.

    Returns:
        str: {"items":[...], "has_more":bool, "next_cursor":str|null}.
    """
    return await _firm_list("customers")(params)


@mcp.tool(
    name="pennylane_firm_list_suppliers",
    annotations={"title": "Firm: list a client company's suppliers", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_firm_list_suppliers(params: FirmListInput) -> str:
    """List a client company's suppliers via the Firm API.

    Requires the suppliers:readonly firm scope.

    Returns:
        str: {"items":[...], "has_more":bool, "next_cursor":str|null}.
    """
    return await _firm_list("suppliers")(params)


@mcp.tool(
    name="pennylane_firm_list_journals",
    annotations={"title": "Firm: list a client company's journals", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_firm_list_journals(params: FirmListInput) -> str:
    """List a client company's accounting journals via the Firm API.

    Requires the journals:readonly (or journals:all) firm scope.

    Returns:
        str: {"items":[...], "has_more":bool, "next_cursor":str|null}.
    """
    return await _firm_list("journals")(params)


@mcp.tool(
    name="pennylane_firm_list_ledger_accounts",
    annotations={"title": "Firm: list a client company's ledger accounts",
                 "readOnlyHint": True, "destructiveHint": False,
                 "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_firm_list_ledger_accounts(params: FirmListInput) -> str:
    """List a client company's ledger accounts (plan comptable) via the Firm API.

    Useful to resolve ledger_account_id values for ledger entries. Common
    filterable fields: number, label. Requires the ledger_accounts:readonly
    (or ledger_accounts:all) firm scope.

    Returns:
        str: {"items":[...], "has_more":bool, "next_cursor":str|null}.
    """
    return await _firm_list("ledger_accounts")(params)


@mcp.tool(
    name="pennylane_firm_list_ledger_entries",
    annotations={"title": "Firm: list a client company's ledger entries",
                 "readOnlyHint": True, "destructiveHint": False,
                 "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_firm_list_ledger_entries(params: FirmListInput) -> str:
    """List a client company's ledger entries via the Firm API.

    By default, entries from closed/frozen fiscal periods are excluded — but
    providing a `date` filter returns all entries in the range regardless.
    For a full-book extract prefer pennylane_firm_create_export. Requires the
    ledger_entries:readonly (or ledger_entries:all) firm scope.

    Returns:
        str: {"items":[...], "has_more":bool, "next_cursor":str|null}.
    """
    return await _firm_list("ledger_entries")(params)


@mcp.tool(
    name="pennylane_firm_list_ledger_entry_lines",
    annotations={"title": "Firm: list a client company's ledger entry lines",
                 "readOnlyHint": True, "destructiveHint": False,
                 "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_firm_list_ledger_entry_lines(params: FirmListInput) -> str:
    """List a client company's ledger entry lines via the Firm API.

    Requires the ledger_entries:readonly (or ledger_entries:all) firm scope.

    Returns:
        str: {"items":[...], "has_more":bool, "next_cursor":str|null}.
    """
    return await _firm_list("ledger_entry_lines")(params)


@mcp.tool(
    name="pennylane_firm_list_fiscal_years",
    annotations={"title": "Firm: list a client company's fiscal years",
                 "readOnlyHint": True, "destructiveHint": False,
                 "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_firm_list_fiscal_years(params: FirmListInput) -> str:
    """List a client company's fiscal years via the Firm API.

    Requires the fiscal_years:readonly (or fiscal_years:all) firm scope.

    Returns:
        str: {"items":[...], "has_more":bool, "next_cursor":str|null}.
    """
    return await _firm_list("fiscal_years")(params)


class FirmTrialBalanceInput(_Base):
    """Inputs for a client company's trial balance (page-based pagination)."""
    period_start: str = Field(..., description="Start of period, YYYY-MM-DD.")
    period_end: str = Field(..., description="End of period, YYYY-MM-DD.")
    company_id: Optional[int] = Field(
        default=None, description="Client company ID. Omit to use the configured default."
    )
    is_auxiliary: Optional[bool] = Field(
        default=None, description="Include auxiliary (sub-)accounts."
    )
    page: Optional[int] = Field(default=None, ge=1, description="Page number, starting at 1.")
    per_page: Optional[int] = Field(
        default=None, ge=1, le=1000, description="Items per page (default 500)."
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)


@mcp.tool(
    name="pennylane_firm_get_trial_balance",
    annotations={"title": "Firm: get a client company's trial balance",
                 "readOnlyHint": True, "destructiveHint": False,
                 "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_firm_get_trial_balance(params: FirmTrialBalanceInput) -> str:
    """Get a client company's trial balance (balance générale) for a period.

    Returns balances per ledger account (amounts are strings). NOTE: paginates
    with page/per_page (default 500 per page), not with a cursor. Requires the
    trial_balance:readonly firm scope.

    Args:
        params.period_start (str), params.period_end (str): Period, YYYY-MM-DD.
        params.is_auxiliary (Optional[bool]): Include auxiliary accounts.

    Returns:
        str: JSON trial balance rows, or an error string.
    """
    try:
        query: dict[str, Any] = {
            "period_start": params.period_start,
            "period_end": params.period_end,
        }
        if params.is_auxiliary is not None:
            query["is_auxiliary"] = params.is_auxiliary
        if params.page is not None:
            query["page"] = params.page
        if params.per_page is not None:
            query["per_page"] = params.per_page
        data = await _firm_request(
            "GET", _firm_company_path(params.company_id, "trial_balance"), query)
        return _format(data, params.response_format)
    except PennylaneError as exc:
        return str(exc)


class FirmGenericGetInput(_Base):
    """Inputs for an arbitrary read-only GET against a client company (firm API)."""
    path: str = Field(
        ...,
        description="Path under companies/{company_id}/, e.g. 'categories', "
                    "'category_groups', 'dms/files', 'dms/folders', "
                    "'journals/123', 'exports/fecs/45', "
                    "'changelogs/ledger_entry_lines', "
                    "'ledger_entry_lines/9/lettered_ledger_entry_lines', "
                    "'bank_accounts', 'transactions'. No host, no company prefix.",
        min_length=1,
    )
    company_id: Optional[int] = Field(
        default=None, description="Client company ID. Omit to use the configured default."
    )
    filters: Optional[list[dict[str, Any]]] = Field(
        default=None, description="Optional filter array ({field,operator,value})."
    )
    query: Optional[dict[str, Any]] = Field(
        default=None, description="Additional raw query params (e.g. {'cursor':'..','limit':50})."
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)


@mcp.tool(
    name="pennylane_firm_get",
    annotations={"title": "Firm: generic GET (any firm endpoint of a client company)",
                 "readOnlyHint": True, "destructiveHint": False,
                 "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_firm_get(params: FirmGenericGetInput) -> str:
    """Read-only access to ANY company-scoped Firm API GET endpoint not covered
    by a dedicated tool (categories, category_groups, DMS files/folders,
    changelogs, single resources by ID, export status polling, bank_accounts,
    transactions, lettered ledger entry lines, ...).

    Only GET requests are issued — it can never modify data. The path is always
    resolved under companies/{company_id}/ so it cannot escape the selected
    client company.

    Args:
        params.path (str): Path under companies/{company_id}/ (see examples).
        params.filters (Optional[list]): Filter array, applied as `filter`.
        params.query (Optional[dict]): Extra query params (cursor, limit, dates...).

    Returns:
        str: Raw JSON response from the endpoint, or an error string.

    Examples:
        - Categories: path="categories"
        - One journal: path="journals/123"
        - Poll a FEC export: path="exports/fecs/45"
        - DMS files: path="dms/files"
        - Ledger changelog: path="changelogs/ledger_entry_lines",
          query={"start_date":"2026-06-01"}
    """
    try:
        clean = params.path.strip().lstrip("/")
        if not clean or ".." in clean.split("/"):
            return (f"Error: invalid path '{params.path}'. Give a relative path under "
                    "companies/{company_id}/, e.g. 'categories' or 'journals/123'.")
        merged = dict(params.query or {})
        if params.filters:
            merged["filter"] = json.dumps(params.filters)
        data = await _firm_request(
            "GET", _firm_company_path(params.company_id, clean), merged or None)
        return _format(data, params.response_format)
    except PennylaneError as exc:
        return str(exc)


# --- Firm writes: accounting structure, entries, transactions, files ------- #

class FirmCreateJournalInput(_Base):
    """Create an accounting journal in a client company (firm API)."""
    code: str = Field(..., description="Short journal code, e.g. 'VE'.", min_length=1)
    label: str = Field(..., description="Journal name, e.g. 'Sales'.", min_length=1)
    company_id: Optional[int] = Field(
        default=None, description="Client company ID. Omit to use the configured default."
    )
    extra_fields: Optional[dict[str, Any]] = Field(
        default=None, description="Any other journal fields passed straight through."
    )


@mcp.tool(
    name="pennylane_firm_create_journal",
    annotations={"title": "Firm: create a journal", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_firm_create_journal(params: FirmCreateJournalInput) -> str:
    """Create an accounting journal in a client company (firm API).

    Unlike Company v2, the firm endpoint requires BOTH `code` and `label`.
    Requires the journals:all firm scope.

    Returns:
        str: JSON of the created journal (with its internal id), or an error string.
    """
    try:
        payload: dict[str, Any] = {"code": params.code, "label": params.label}
        if params.extra_fields:
            payload.update(params.extra_fields)
        data = await _firm_request(
            "POST", _firm_company_path(params.company_id, "journals"), json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class FirmCreateLedgerAccountInput(_Base):
    """Create a ledger account in a client company (firm API)."""
    number: str = Field(..., description="Account number, e.g. '706000'.", min_length=1)
    label: str = Field(..., description="Account label.", min_length=1)
    company_id: Optional[int] = Field(
        default=None, description="Client company ID. Omit to use the configured default."
    )
    extra_fields: Optional[dict[str, Any]] = Field(
        default=None,
        description="Any other account fields passed through (vat_rate, country_alpha2).",
    )


@mcp.tool(
    name="pennylane_firm_create_ledger_account",
    annotations={"title": "Firm: create a ledger account", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_firm_create_ledger_account(params: FirmCreateLedgerAccountInput) -> str:
    """Create a ledger account (plan comptable entry) in a client company.

    Requires the ledger_accounts:all firm scope.

    Returns:
        str: JSON of the created ledger account (with its internal id), or an error.
    """
    try:
        payload: dict[str, Any] = {"number": params.number, "label": params.label}
        if params.extra_fields:
            payload.update(params.extra_fields)
        data = await _firm_request(
            "POST", _firm_company_path(params.company_id, "ledger_accounts"),
            json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class FirmUpdateByIdInput(_Base):
    """Update a client-company resource by ID (firm API)."""
    id: str = Field(..., description="Internal ID of the resource to update.", min_length=1)
    fields: dict[str, Any] = Field(
        ...,
        description="Object of fields to change. Include only what you want to "
                    "modify. Monetary amounts must be sent as STRINGS.",
    )
    company_id: Optional[int] = Field(
        default=None, description="Client company ID. Omit to use the configured default."
    )


@mcp.tool(
    name="pennylane_firm_update_ledger_account",
    annotations={"title": "Firm: update a ledger account", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_firm_update_ledger_account(params: FirmUpdateByIdInput) -> str:
    """Update a client company's ledger account (PUT — common fields: label,
    letterable). Requires the ledger_accounts:all firm scope.

    Returns:
        str: JSON of the updated ledger account, or an error string.
    """
    try:
        data = await _firm_request(
            "PUT", _firm_company_path(params.company_id, f"ledger_accounts/{params.id}"),
            json_body=params.fields)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class FirmCreateLedgerEntryInput(_Base):
    """Create a balanced ledger entry in a client company (firm API)."""
    date: str = Field(..., description="Entry date, YYYY-MM-DD.")
    label: str = Field(..., description="Entry label / description (required here, "
                                        "unlike Company v2).", min_length=1)
    journal_id: int = Field(..., description="Journal internal ID (see "
                                             "pennylane_firm_list_journals).")
    ledger_entry_lines: list[dict[str, Any]] = Field(
        ...,
        description='Double-entry lines (>=2). Each: {"ledger_account_id":int,'
                    '"label":str,"debit":"<string>","credit":"<string>"}. Amounts '
                    'are STRINGS; total debits MUST equal total credits. Put "0" on '
                    "the unused side of each line.",
        min_length=2,
    )
    company_id: Optional[int] = Field(
        default=None, description="Client company ID. Omit to use the configured default."
    )
    extra_fields: Optional[dict[str, Any]] = Field(
        default=None,
        description="Any other entry fields passed through (currency, file_attachment_id).",
    )


@mcp.tool(
    name="pennylane_firm_create_ledger_entry",
    annotations={"title": "Firm: create a ledger entry", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_firm_create_ledger_entry(params: FirmCreateLedgerEntryInput) -> str:
    """Create a balanced ledger (journal) entry in a client company.

    Amounts are STRINGS and total debits must equal total credits, or the API
    returns 422. Reference accounts by ledger_account_id (not account number) —
    resolve them with pennylane_firm_list_ledger_accounts. Requires the
    ledger_entries:all firm scope.

    Returns:
        str: JSON of the created ledger entry (with its internal id), or an error.
    """
    try:
        payload: dict[str, Any] = {
            "date": params.date,
            "label": params.label,
            "journal_id": params.journal_id,
            "ledger_entry_lines": params.ledger_entry_lines,
        }
        if params.extra_fields:
            payload.update(params.extra_fields)
        data = await _firm_request(
            "POST", _firm_company_path(params.company_id, "ledger_entries"),
            json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_firm_update_ledger_entry",
    annotations={"title": "Firm: update a ledger entry", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_firm_update_ledger_entry(params: FirmUpdateByIdInput) -> str:
    """Update a client company's ledger entry (PUT — common fields: date, label,
    journal_id, ledger_entry_lines with string amounts, debits = credits).
    Requires the ledger_entries:all firm scope.

    Returns:
        str: JSON of the updated ledger entry, or an error string.
    """
    try:
        data = await _firm_request(
            "PUT", _firm_company_path(params.company_id, f"ledger_entries/{params.id}"),
            json_body=params.fields)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class FirmCreateFiscalYearInput(_Base):
    """Create a fiscal year in a client company (firm API)."""
    start: str = Field(..., description="Fiscal year start date, YYYY-MM-DD.")
    finish: str = Field(..., description="Fiscal year end date, YYYY-MM-DD.")
    company_id: Optional[int] = Field(
        default=None, description="Client company ID. Omit to use the configured default."
    )


@mcp.tool(
    name="pennylane_firm_create_fiscal_year",
    annotations={"title": "Firm: create a fiscal year", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_firm_create_fiscal_year(params: FirmCreateFiscalYearInput) -> str:
    """Create a fiscal year in a client company. Fiscal years must be
    consecutive and cannot overlap. Requires the fiscal_years:all firm scope.

    Returns:
        str: JSON of the created fiscal year, or an error string.
    """
    try:
        payload = {"start": params.start, "finish": params.finish}
        data = await _firm_request(
            "POST", _firm_company_path(params.company_id, "fiscal_years"),
            json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class FirmCreateTransactionInput(_Base):
    """Create a bank transaction in a client company (firm API)."""
    bank_account_id: int = Field(..., description="Bank account internal ID.")
    label: str = Field(..., description="Transaction label.", min_length=1)
    date: str = Field(..., description="Transaction date, YYYY-MM-DD.")
    amount: str = Field(..., description='Amount as a STRING, e.g. "125.50" '
                                         '(negative for debits).')
    company_id: Optional[int] = Field(
        default=None, description="Client company ID. Omit to use the configured default."
    )
    extra_fields: Optional[dict[str, Any]] = Field(
        default=None, description="Any other transaction fields passed through (fee)."
    )


@mcp.tool(
    name="pennylane_firm_create_transaction",
    annotations={"title": "Firm: create a bank transaction", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_firm_create_transaction(params: FirmCreateTransactionInput) -> str:
    """Create a banking transaction in a client company (firm API).

    Requires the transactions:all firm scope.

    Returns:
        str: JSON of the created transaction (with its internal id), or an error.
    """
    try:
        payload: dict[str, Any] = {
            "bank_account_id": params.bank_account_id,
            "label": params.label,
            "date": params.date,
            "amount": params.amount,
        }
        if params.extra_fields:
            payload.update(params.extra_fields)
        data = await _firm_request(
            "POST", _firm_company_path(params.company_id, "transactions"),
            json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


@mcp.tool(
    name="pennylane_firm_update_transaction",
    annotations={"title": "Firm: update a bank transaction", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def pennylane_firm_update_transaction(params: FirmUpdateByIdInput) -> str:
    """Update a client company's bank transaction (PUT). Requires the
    transactions:all firm scope.

    Returns:
        str: JSON of the updated transaction, or an error string.
    """
    try:
        data = await _firm_request(
            "PUT", _firm_company_path(params.company_id, f"transactions/{params.id}"),
            json_body=params.fields)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class FirmCreateWithFieldsInput(_Base):
    """Create a client-company resource by passing the full payload (firm API)."""
    fields: dict[str, Any] = Field(
        ..., description="The full create payload as an object. Monetary amounts as strings."
    )
    company_id: Optional[int] = Field(
        default=None, description="Client company ID. Omit to use the configured default."
    )


@mcp.tool(
    name="pennylane_firm_create_bank_account",
    annotations={"title": "Firm: create a bank account", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_firm_create_bank_account(params: FirmCreateWithFieldsInput) -> str:
    """Create a bank account in a client company (firm API).

    Pass the payload in `fields` — `name` is required; common extras: iban, bic,
    currency, account_type, bank_establishment_id. Requires the
    bank_accounts:all firm scope.

    Returns:
        str: JSON of the created bank account (with its internal id), or an error.
    """
    try:
        data = await _firm_request(
            "POST", _firm_company_path(params.company_id, "bank_accounts"),
            json_body=params.fields)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class FirmExportKind(str, Enum):
    """Which accounting export the firm API can generate."""
    FEC = "fec"
    ANALYTICAL_GENERAL_LEDGER = "analytical_general_ledger"


_FIRM_EXPORT_PATHS = {
    FirmExportKind.FEC: "exports/fecs",
    FirmExportKind.ANALYTICAL_GENERAL_LEDGER: "exports/analytical_general_ledgers",
}


class FirmCreateExportInput(_Base):
    """Trigger an accounting export for a client company (firm API)."""
    kind: FirmExportKind = Field(
        ...,
        description="Which export to generate: 'fec' (Fichier des Écritures "
                    "Comptables) or 'analytical_general_ledger'. (The plain "
                    "general_ledger export exists only on Company v2.)",
    )
    period_start: str = Field(..., description="Start of period, YYYY-MM-DD.")
    period_end: str = Field(..., description="End of period, YYYY-MM-DD.")
    company_id: Optional[int] = Field(
        default=None, description="Client company ID. Omit to use the configured default."
    )
    mode: Optional[str] = Field(
        default=None,
        description="Only for analytical_general_ledger: 'in_line' (default) or "
                    "'in_column'. Ignored for FEC.",
    )


@mcp.tool(
    name="pennylane_firm_create_export",
    annotations={"title": "Firm: create an accounting export (FEC / AGL)",
                 "readOnlyHint": False, "destructiveHint": False,
                 "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_firm_create_export(params: FirmCreateExportInput) -> str:
    """Trigger a FEC or analytical general ledger export for a client company.

    Exports are asynchronous: this returns an export object with an `id` and a
    `status`. Poll it with pennylane_firm_get (path e.g. `exports/fecs/{id}` or
    `exports/analytical_general_ledgers/{id}`) until the download URL appears.
    Requires the matching firm scope (exports:fec or exports:agl).

    Returns:
        str: JSON of the created export (with its `id` and `status`), or an error.
    """
    try:
        payload: dict[str, Any] = {
            "period_start": params.period_start,
            "period_end": params.period_end,
        }
        if params.kind == FirmExportKind.ANALYTICAL_GENERAL_LEDGER and params.mode:
            payload["mode"] = params.mode
        data = await _firm_request(
            "POST", _firm_company_path(params.company_id, _FIRM_EXPORT_PATHS[params.kind]),
            json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class FirmUploadFileInput(_Base):
    """Upload a local file to a client company as a file_attachment (firm API)."""
    file_path: str = Field(..., description="Absolute path to a local file.", min_length=1)
    company_id: Optional[int] = Field(
        default=None, description="Client company ID. Omit to use the configured default."
    )


def _local_file_part(file_path: str) -> tuple[str, bytes, str] | str:
    """Read a local file for multipart upload; return an error string if missing."""
    p = Path(file_path).expanduser()
    if not p.is_file():
        return f"Error: no file found at '{file_path}'. Provide an absolute path."
    content_type = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return (p.name, p.read_bytes(), content_type)


@mcp.tool(
    name="pennylane_firm_upload_file_attachment",
    annotations={"title": "Firm: upload a file attachment", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_firm_upload_file_attachment(params: FirmUploadFileInput) -> str:
    """Upload a local file to a client company (multipart POST file_attachments).

    Returns a file_attachment whose `id` can be referenced by other resources
    (e.g. a ledger entry's file_attachment_id). Max 100 MB. This does NOT put
    the file in the DMS — use pennylane_firm_upload_dms_file for that.
    Requires the file_attachments:all firm scope.

    Returns:
        str: JSON of the uploaded attachment (with its `id`), or an error string.
    """
    try:
        part = _local_file_part(params.file_path)
        if isinstance(part, str):
            return part
        data = await _firm_request(
            "POST", _firm_company_path(params.company_id, "file_attachments"),
            files={"file": part})
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class FirmUploadDmsFileInput(_Base):
    """Upload a local file into a client company's DMS / GED (firm API)."""
    file_path: str = Field(..., description="Absolute path to a local file.", min_length=1)
    company_id: Optional[int] = Field(
        default=None, description="Client company ID. Omit to use the configured default."
    )
    name: Optional[str] = Field(
        default=None, description="Display name in the DMS (defaults to the filename)."
    )
    parent_folder_id: Optional[int] = Field(
        default=None,
        description="DMS folder to file it under (see pennylane_firm_get "
                    "path='dms/folders'). Omit for the root.",
    )


@mcp.tool(
    name="pennylane_firm_upload_dms_file",
    annotations={"title": "Firm: upload a file to the DMS (GED)", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_firm_upload_dms_file(params: FirmUploadDmsFileInput) -> str:
    """Upload a local file into a client company's DMS (GED) via the firm API.

    Requires the dms_files:all firm scope.

    Returns:
        str: JSON of the created DMS file, or an error string.
    """
    try:
        part = _local_file_part(params.file_path)
        if isinstance(part, str):
            return part
        files: dict[str, Any] = {"file": part}
        if params.name:
            files["name"] = (None, params.name)
        if params.parent_folder_id is not None:
            files["parent_folder_id"] = (None, str(params.parent_folder_id))
        data = await _firm_request(
            "POST", _firm_company_path(params.company_id, "dms/files"), files=files)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


class FirmCreateDmsFolderInput(_Base):
    """Create a DMS folder in a client company (firm API)."""
    name: str = Field(..., description="Folder name.", min_length=1)
    company_id: Optional[int] = Field(
        default=None, description="Client company ID. Omit to use the configured default."
    )
    parent_folder_id: Optional[int] = Field(
        default=None, description="Parent DMS folder ID. Omit for the root."
    )


@mcp.tool(
    name="pennylane_firm_create_dms_folder",
    annotations={"title": "Firm: create a DMS folder", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pennylane_firm_create_dms_folder(params: FirmCreateDmsFolderInput) -> str:
    """Create a DMS (GED) folder in a client company via the firm API.

    Requires the dms_files:all firm scope.

    Returns:
        str: JSON of the created folder (with its internal id), or an error string.
    """
    try:
        payload: dict[str, Any] = {"name": params.name}
        if params.parent_folder_id is not None:
            payload["parent_folder_id"] = params.parent_folder_id
        data = await _firm_request(
            "POST", _firm_company_path(params.company_id, "dms/folders"),
            json_body=payload)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except PennylaneError as exc:
        return str(exc)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def _print_startup_info() -> None:
    """Print non-secret startup info to stderr (safe for stdio transport)."""
    names = ", ".join(sorted(COMPANIES)) if COMPANIES else "(none configured)"
    firm = "configured" if FIRM_TOKEN else "(not configured)"
    if FIRM_TOKEN and FIRM_DEFAULT_COMPANY_ID is not None:
        firm += f", default company_id={FIRM_DEFAULT_COMPANY_ID}"
    print(f"[pennylane_mcp] base_url={API_BASE_URL}", file=sys.stderr)
    print(f"[pennylane_mcp] companies={names}", file=sys.stderr)
    print(f"[pennylane_mcp] firm_api={firm} (base_url={FIRM_API_BASE_URL})", file=sys.stderr)
    print(f"[pennylane_mcp] use_2026_changes={USE_2026_CHANGES}", file=sys.stderr)


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        print("\nConfigured companies:", ", ".join(sorted(COMPANIES)) or "(none)")
        print("\nRun with no arguments to start the MCP server over stdio.")
        sys.exit(0)
    _print_startup_info()
    mcp.run()

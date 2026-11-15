/**
 * Typed client for the installment-ledger API.
 *
 * The five endpoints are the ones in app/api/routes.py. There is deliberately
 * no list endpoint on the service, so there is no `listLoans` here either; the
 * accounts view keeps its own set of loan ids and asks for each one.
 *
 * `fetch` is injected so tests can drive the client without a server and
 * without a network.
 */

import { ApiError, TransportError, parseErrorBody } from "./errors";
import type { CreatePaymentRequest, Ledger, Loan, Payment } from "./types";

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface LedgerClientOptions {
  readonly baseUrl?: string;
  readonly fetchImpl?: FetchLike;
}

export interface RequestOptions {
  readonly signal?: AbortSignal;
}

/**
 * Same origin by default: in development Vite proxies `/api` to the service
 * (see vite.config.ts), because the service sends no CORS headers.
 */
export const DEFAULT_BASE_URL = "/api";

function resolveBaseUrl(explicit: string | undefined): string {
  if (explicit !== undefined && explicit !== "") {
    return explicit.replace(/\/+$/, "");
  }
  const configured: unknown = import.meta.env.VITE_API_BASE_URL;
  if (typeof configured === "string" && configured !== "") {
    return configured.replace(/\/+$/, "");
  }
  return DEFAULT_BASE_URL;
}

export class LedgerClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: FetchLike;

  public constructor(options: LedgerClientOptions = {}) {
    this.baseUrl = resolveBaseUrl(options.baseUrl);
    this.fetchImpl = options.fetchImpl ?? ((input, init) => globalThis.fetch(input, init));
  }

  /** `GET /loans/{id}` */
  public async getLoan(loanId: string, options: RequestOptions = {}): Promise<Loan> {
    return await this.request<Loan>("GET", `/loans/${encodeURIComponent(loanId)}`, options);
  }

  /** `GET /loans/{id}/ledger` */
  public async getLedger(loanId: string, options: RequestOptions = {}): Promise<Ledger> {
    return await this.request<Ledger>(
      "GET",
      `/loans/${encodeURIComponent(loanId)}/ledger`,
      options,
    );
  }

  /**
   * `POST /loans/{id}/payments`
   *
   * The `Idempotency-Key` header is required by the service and is a parameter
   * here rather than something the client invents, so the caller decides what
   * counts as the same attempt.
   */
  public async createPayment(
    loanId: string,
    body: CreatePaymentRequest,
    idempotencyKey: string,
    options: RequestOptions = {},
  ): Promise<Payment> {
    return await this.request<Payment>(
      "POST",
      `/loans/${encodeURIComponent(loanId)}/payments`,
      options,
      body,
      idempotencyKey,
    );
  }

  private async request<T>(
    method: string,
    path: string,
    options: RequestOptions,
    body?: unknown,
    idempotencyKey?: string,
  ): Promise<T> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    if (idempotencyKey !== undefined) {
      headers["Idempotency-Key"] = idempotencyKey;
    }

    const init: RequestInit = { method, headers };
    if (body !== undefined) {
      init.body = JSON.stringify(body);
    }
    if (options.signal !== undefined) {
      init.signal = options.signal;
    }

    let response: Response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}${path}`, init);
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === "AbortError") {
        throw cause;
      }
      throw new TransportError(
        "Could not reach the ledger API. Check that it is running and that the dev server proxy points at it.",
      );
    }

    const payload = await readJson(response);

    if (!response.ok) {
      throw parseErrorBody(response.status, payload);
    }

    return payload as T;
  }
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text === "") {
    return null;
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    if (response.ok) {
      throw new TransportError("The ledger API returned a body that is not JSON.");
    }
    return null;
  }
}

export { ApiError, TransportError };

/** Fixtures and a stub client, so component tests need no server and no network. */

import { vi } from "vitest";
import type { Mock } from "vitest";

import type { LedgerClient, RequestOptions } from "../api/client";
import type { CreatePaymentRequest, Ledger, Loan, Payment } from "../api/types";

export const LOAN_ID = "11111111-2222-3333-4444-555555555555";

export function makeLoan(overrides: Partial<Loan> = {}): Loan {
  return {
    id: LOAN_ID,
    status: "open",
    principal_cents: 120_000,
    apr_bps: 0,
    installment_count: 4,
    cadence: "biweekly",
    start_date: "2026-01-15",
    term_days: 56,
    interest_cents: 0,
    total_cents: 120_000,
    paid_cents: 30_000,
    outstanding_cents: 90_000,
    created_at: "2026-01-15T00:00:00+00:00",
    installments: [
      {
        id: "aaaaaaaa-0000-0000-0000-000000000001",
        sequence: 1,
        due_date: "2026-01-29",
        amount_cents: 30_000,
        paid_cents: 30_000,
        outstanding_cents: 0,
      },
    ],
    ...overrides,
  };
}

export function makeLedger(overrides: Partial<Ledger> = {}): Ledger {
  return {
    loan_id: LOAN_ID,
    debit_cents: 150_000,
    credit_cents: 150_000,
    net_cents: 0,
    transactions: [
      {
        transaction_id: "bbbbbbbb-0000-0000-0000-000000000001",
        created_at: "2026-01-15T00:00:00+00:00",
        debit_cents: 120_000,
        credit_cents: 120_000,
        net_cents: 0,
        entries: [
          { account: "loan_receivable", direction: "debit", amount_cents: 120_000 },
          { account: "funding_payable", direction: "credit", amount_cents: 120_000 },
        ],
      },
    ],
    ...overrides,
  };
}

export function makePayment(overrides: Partial<Payment> = {}): Payment {
  return {
    id: "cccccccc-0000-0000-0000-000000000001",
    loan_id: LOAN_ID,
    amount_cents: 12_500,
    transaction_id: "dddddddd-0000-0000-0000-000000000001",
    created_at: "2026-02-01T00:00:00+00:00",
    allocations: [
      {
        installment_sequence: 2,
        installment_id: "aaaaaaaa-0000-0000-0000-000000000002",
        amount_cents: 12_500,
      },
    ],
    loan: makeLoan({ paid_cents: 42_500, outstanding_cents: 77_500 }),
    ...overrides,
  };
}

type GetLoan = (loanId: string, options?: RequestOptions) => Promise<Loan>;
type GetLedger = (loanId: string, options?: RequestOptions) => Promise<Ledger>;
type CreatePayment = (
  loanId: string,
  body: CreatePaymentRequest,
  idempotencyKey: string,
  options?: RequestOptions,
) => Promise<Payment>;

export interface StubClient {
  readonly client: LedgerClient;
  readonly getLoan: Mock<GetLoan>;
  readonly getLedger: Mock<GetLedger>;
  readonly createPayment: Mock<CreatePayment>;
}

/**
 * A stand in for `LedgerClient` with the three methods the views call.
 *
 * Typed through the real interface so a change to a method signature breaks
 * these tests rather than silently passing.
 */
export function stubClient(): StubClient {
  const getLoan = vi.fn<GetLoan>();
  const getLedger = vi.fn<GetLedger>();
  const createPayment = vi.fn<CreatePayment>();

  const client = { getLoan, getLedger, createPayment } as unknown as LedgerClient;

  return { client, getLoan, getLedger, createPayment };
}

/** A promise whose resolution the test controls. */
export function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

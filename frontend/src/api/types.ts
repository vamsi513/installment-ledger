/**
 * Response and request shapes for the installment-ledger API.
 *
 * These mirror the backend by hand rather than by code generation: generating
 * would need a running service and a generator dependency, and the surface is
 * five endpoints. Each type below names the backend definition it follows, so
 * a drift is a one line diff to find.
 *
 *   requests   app/schemas.py
 *   responses  app/presenters.py
 *   enums      app/models.py, app/schedule.py
 *
 * Every amount is an integer number of cents, exactly as the API sends it.
 */

/** app/models.py :: LoanStatus */
export type LoanStatus = "open" | "closed";

/** app/schedule.py :: Cadence */
export type Cadence = "biweekly" | "monthly";

/** app/models.py :: LedgerAccount */
export type LedgerAccount = "loan_receivable" | "funding_payable" | "interest_income" | "cash";

/** app/models.py :: EntryDirection */
export type EntryDirection = "debit" | "credit";

/** app/presenters.py :: installment_body */
export interface Installment {
  readonly id: string;
  readonly sequence: number;
  readonly due_date: string;
  readonly amount_cents: number;
  readonly paid_cents: number;
  readonly outstanding_cents: number;
}

/** app/presenters.py :: loan_body */
export interface Loan {
  readonly id: string;
  readonly status: LoanStatus;
  readonly principal_cents: number;
  readonly apr_bps: number;
  readonly installment_count: number;
  readonly cadence: Cadence;
  readonly start_date: string;
  readonly term_days: number;
  readonly interest_cents: number;
  readonly total_cents: number;
  readonly paid_cents: number;
  readonly outstanding_cents: number;
  readonly created_at: string;
  readonly installments: readonly Installment[];
}

/** app/presenters.py :: payment_body, one allocation */
export interface PaymentAllocation {
  readonly installment_sequence: number;
  readonly installment_id: string;
  readonly amount_cents: number;
}

/** app/presenters.py :: payment_body */
export interface Payment {
  readonly id: string;
  readonly loan_id: string;
  readonly amount_cents: number;
  readonly transaction_id: string;
  readonly created_at: string;
  readonly allocations: readonly PaymentAllocation[];
  readonly loan: Loan;
}

/** app/presenters.py :: ledger_body, one entry */
export interface LedgerEntry {
  readonly account: LedgerAccount;
  readonly direction: EntryDirection;
  readonly amount_cents: number;
}

/** app/presenters.py :: ledger_body, one transaction */
export interface LedgerTransaction {
  readonly transaction_id: string;
  readonly created_at: string;
  readonly debit_cents: number;
  readonly credit_cents: number;
  readonly net_cents: number;
  readonly entries: readonly LedgerEntry[];
}

/** app/presenters.py :: ledger_body */
export interface Ledger {
  readonly loan_id: string;
  readonly debit_cents: number;
  readonly credit_cents: number;
  readonly net_cents: number;
  readonly transactions: readonly LedgerTransaction[];
}

/** app/schemas.py :: CreatePaymentRequest */
export interface CreatePaymentRequest {
  readonly amount_cents: number;
}

// app/schemas.py :: CreatePaymentRequest field bounds. Checked in the browser
// so an obviously bad amount is named before a request is spent on it; the
// backend remains the authority and rejects anything these miss.
export const PAYMENT_MIN_CENTS = 1;
export const PAYMENT_MAX_CENTS = 100_000_000;

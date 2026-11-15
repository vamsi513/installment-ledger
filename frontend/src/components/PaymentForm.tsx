/**
 * Post a transaction against one loan.
 *
 * A transaction is not something the API accepts directly: a payment is, and
 * applying one writes the balanced pair of ledger entries that forms a
 * transaction. So this form posts `POST /loans/{id}/payments`.
 *
 * Three things matter here.
 *
 * The reader types dollars; the request carries integer cents. The conversion
 * happens once, in parseDollarsToCents, and a string that is not a whole
 * number of cents is refused rather than rounded.
 *
 * Each submitted attempt gets one fresh idempotency key.
 *
 * A second click cannot post a second payment. The guard is a ref read and set
 * synchronously inside the submit handler, so it holds even if two clicks land
 * in the same React batch and no re-render has happened in between. The
 * disabled button is only the visible half of that.
 */

import { useCallback, useRef, useState } from "react";
import type { JSX } from "react";

import type { LedgerClient } from "../api/client";
import type { Payment } from "../api/types";
import { PAYMENT_MAX_CENTS, PAYMENT_MIN_CENTS } from "../api/types";
import { newIdempotencyKey } from "../lib/idempotency";
import { MoneyFormatError, formatCents, parseDollarsToCents } from "../lib/money";
import { ErrorNotice } from "./StatusNotice";

export interface PaymentFormProps {
  readonly client: LedgerClient;
  readonly loanId: string | null;
  readonly onPosted?: (payment: Payment) => void;
}

export function PaymentForm({ client, loanId, onPosted }: PaymentFormProps): JSX.Element {
  const [amount, setAmount] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<unknown>(null);
  const [lastKey, setLastKey] = useState<string | null>(null);
  const [posted, setPosted] = useState<Payment | null>(null);

  // Synchronous double submit guard. State would be a render behind.
  const inFlight = useRef(false);

  const handleSubmit = useCallback(
    (event: React.FormEvent<HTMLFormElement>) => {
      event.preventDefault();

      if (inFlight.current) {
        return;
      }

      if (loanId === null || loanId === "") {
        setLocalError("Choose a loan on the accounts list first.");
        return;
      }

      let amountCents: number;
      try {
        amountCents = parseDollarsToCents(amount);
      } catch (error) {
        setLocalError(
          error instanceof MoneyFormatError ? error.message : "That amount could not be read.",
        );
        return;
      }

      if (amountCents < PAYMENT_MIN_CENTS) {
        setLocalError("A payment has to be at least one cent.");
        return;
      }
      if (amountCents > PAYMENT_MAX_CENTS) {
        setLocalError(`A payment cannot exceed ${formatCents(PAYMENT_MAX_CENTS)}.`);
        return;
      }

      // One key per submitted attempt.
      const idempotencyKey = newIdempotencyKey();

      inFlight.current = true;
      setSubmitting(true);
      setLocalError(null);
      setRequestError(null);
      setPosted(null);
      setLastKey(idempotencyKey);

      client
        .createPayment(loanId, { amount_cents: amountCents }, idempotencyKey)
        .then((payment) => {
          setPosted(payment);
          setAmount("");
          onPosted?.(payment);
        })
        .catch((error: unknown) => {
          setRequestError(error);
        })
        .finally(() => {
          inFlight.current = false;
          setSubmitting(false);
        });
    },
    [amount, client, loanId, onPosted],
  );

  const disabled = submitting || loanId === null || loanId === "";

  return (
    <section aria-labelledby="payment-heading">
      <h2 id="payment-heading">Post a transaction</h2>
      <p className="view-intro">
        A payment against a loan. The service writes the balanced ledger entries, so one payment is
        one transaction.
      </p>

      <form onSubmit={handleSubmit} noValidate>
        {/* A span, not an <output>: this is a static value, and <output> has
            the implicit ARIA role "status", which would compete with the
            posted-payment notice below. */}
        <p className="field">
          <span className="field-label">Loan</span>
          <span id="payment-loan">
            {loanId === null || loanId === "" ? "No loan selected" : <code>{loanId}</code>}
          </span>
        </p>

        <p className="field">
          <label htmlFor="payment-amount">Amount in dollars</label>
          <input
            id="payment-amount"
            name="amount"
            type="text"
            inputMode="decimal"
            autoComplete="off"
            placeholder="125.00"
            value={amount}
            onChange={(event) => {
              setAmount(event.target.value);
            }}
            aria-describedby="payment-amount-hint"
          />
          <span id="payment-amount-hint" className="field-hint">
            Sent to the API as integer cents.
          </span>
        </p>

        <button type="submit" disabled={disabled}>
          {submitting ? "Posting…" : "Post payment"}
        </button>
      </form>

      {localError !== null && (
        <p className="notice notice-error" role="alert">
          {localError}
        </p>
      )}

      {requestError !== null && (
        <>
          <ErrorNotice error={requestError} title="The payment was not accepted" />
          {lastKey !== null && (
            <p className="notice-meta">
              Idempotency key used: <code>{lastKey}</code>
            </p>
          )}
        </>
      )}

      {posted !== null && (
        <div className="notice notice-success" role="status">
          <p>
            Posted {formatCents(posted.amount_cents)} to loan <code>{posted.loan_id}</code>.
          </p>
          <p>
            Transaction <code>{posted.transaction_id}</code>. Outstanding is now{" "}
            <span className="amount">{formatCents(posted.loan.outstanding_cents)}</span>.
          </p>
          {posted.allocations.length > 0 && (
            <ul>
              {posted.allocations.map((allocation) => (
                <li key={allocation.installment_id}>
                  Installment {allocation.installment_sequence}:{" "}
                  <span className="amount">{formatCents(allocation.amount_cents)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

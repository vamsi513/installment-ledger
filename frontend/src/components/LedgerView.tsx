/**
 * The ledger for one loan: every transaction and the entries that make it up.
 *
 * `GET /loans/{id}/ledger` already groups entries by transaction and reports
 * debits, credits and the net for each group and for the loan. The view shows
 * that arithmetic rather than recomputing it, so what is on screen is what the
 * service says.
 *
 * The loading state is derived rather than stored: a request is identified by
 * a token, and anything other than a settled result for the current token is
 * "still loading". That keeps the effect free of synchronous setState calls,
 * which would cascade a second render before the request had even left.
 */

import { useEffect, useState } from "react";
import type { JSX } from "react";

import type { LedgerClient } from "../api/client";
import type { Ledger } from "../api/types";
import { formatCents } from "../lib/money";
import { Empty, ErrorNotice, Loading } from "./StatusNotice";

type Outcome =
  | { readonly kind: "loaded"; readonly ledger: Ledger }
  | { readonly kind: "failed"; readonly error: unknown };

export interface LedgerViewProps {
  readonly client: LedgerClient;
  readonly loanId: string | null;
  /** Changing this value refetches; the payment form bumps it after a post. */
  readonly reloadToken?: number;
}

export function LedgerView({ client, loanId, reloadToken = 0 }: LedgerViewProps): JSX.Element {
  const [settled, setSettled] = useState<{
    readonly token: string;
    readonly outcome: Outcome;
  } | null>(null);

  const token = `${loanId ?? ""}:${String(reloadToken)}`;
  const selected = loanId !== null && loanId !== "";

  useEffect(() => {
    if (!selected) {
      return;
    }

    const controller = new AbortController();

    client
      .getLedger(loanId, { signal: controller.signal })
      .then((ledger) => {
        setSettled({ token, outcome: { kind: "loaded", ledger } });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        setSettled({ token, outcome: { kind: "failed", error } });
      });

    return () => {
      controller.abort();
    };
    // `token` already encodes loanId and reloadToken; listing loanId again
    // would not change when this runs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, token, selected]);

  const outcome: Outcome | null = settled?.token === token ? settled.outcome : null;

  return (
    <section aria-labelledby="ledger-heading">
      <h2 id="ledger-heading">Ledger entries</h2>

      {!selected && <Empty label="Choose a loan on the accounts list to see its ledger." />}
      {selected && outcome === null && <Loading label="Loading the ledger…" />}
      {outcome?.kind === "failed" && (
        <ErrorNotice error={outcome.error} title="The ledger could not be loaded" />
      )}

      {outcome?.kind === "loaded" && (
        <>
          <p className="view-intro">
            Loan <code>{outcome.ledger.loan_id}</code>
          </p>
          <dl className="totals">
            <div>
              <dt>Debits</dt>
              <dd className="amount">{formatCents(outcome.ledger.debit_cents)}</dd>
            </div>
            <div>
              <dt>Credits</dt>
              <dd className="amount">{formatCents(outcome.ledger.credit_cents)}</dd>
            </div>
            <div>
              <dt>Net</dt>
              <dd className="amount">{formatCents(outcome.ledger.net_cents)}</dd>
            </div>
          </dl>

          {outcome.ledger.transactions.length === 0 ? (
            <Empty label="This loan has no ledger entries yet." />
          ) : (
            <ol className="transaction-list">
              {outcome.ledger.transactions.map((transaction) => (
                <li key={transaction.transaction_id} className="transaction">
                  <h3>
                    <code>{transaction.transaction_id}</code>
                  </h3>
                  <p className="transaction-meta">
                    {transaction.created_at} · net{" "}
                    <span className="amount">{formatCents(transaction.net_cents)}</span>
                  </p>
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th scope="col">Account</th>
                        <th scope="col">Direction</th>
                        <th scope="col">Amount</th>
                      </tr>
                    </thead>
                    <tbody>
                      {transaction.entries.map((entry, index) => (
                        <tr key={`${entry.account}-${entry.direction}-${String(index)}`}>
                          <td>{entry.account}</td>
                          <td>{entry.direction}</td>
                          <td className="amount">{formatCents(entry.amount_cents)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </li>
              ))}
            </ol>
          )}
        </>
      )}
    </section>
  );
}

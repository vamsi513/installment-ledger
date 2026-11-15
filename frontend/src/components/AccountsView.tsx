/**
 * The accounts list: every tracked loan with its balances.
 *
 * "Account" in this service is a double entry account name on a ledger entry,
 * not a customer. The thing that carries a balance is a loan, so a loan is
 * what this view lists. There is no list endpoint, so the ids come from the
 * local tracked set and each balance comes from `GET /loans/{id}`.
 *
 * One loan failing is not the page failing: each row settles on its own, so a
 * deleted or mistyped id shows its error beside the loans that did load.
 *
 * As in LedgerView, the loading state is derived from "no settled result for
 * the current request token yet" rather than stored by the effect.
 */

import { useCallback, useEffect, useState } from "react";
import type { JSX } from "react";

import type { LedgerClient } from "../api/client";
import type { Loan } from "../api/types";
import { formatCents } from "../lib/money";
import { isLoanId } from "../lib/trackedLoans";
import { Empty, ErrorNotice, Loading } from "./StatusNotice";

type Outcome =
  | { readonly kind: "loaded"; readonly loan: Loan }
  | { readonly kind: "failed"; readonly error: unknown };

interface Settled {
  readonly token: string;
  readonly outcome: Outcome;
}

export interface AccountsViewProps {
  readonly client: LedgerClient;
  readonly loanIds: readonly string[];
  readonly onTrack: (loanId: string) => void;
  readonly onUntrack: (loanId: string) => void;
  readonly onOpenLedger: (loanId: string) => void;
}

export function AccountsView({
  client,
  loanIds,
  onTrack,
  onUntrack,
  onOpenLedger,
}: AccountsViewProps): JSX.Element {
  const [settled, setSettled] = useState<Readonly<Record<string, Settled>>>({});
  const [draft, setDraft] = useState("");
  const [draftError, setDraftError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  const idList = loanIds.join(",");
  const token = `${String(reloadToken)}:${idList}`;

  useEffect(() => {
    if (loanIds.length === 0) {
      return;
    }

    const controller = new AbortController();

    for (const loanId of loanIds) {
      client
        .getLoan(loanId, { signal: controller.signal })
        .then((loan) => {
          setSettled((previous) => ({
            ...previous,
            [loanId]: { token, outcome: { kind: "loaded", loan } },
          }));
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted) {
            return;
          }
          setSettled((previous) => ({
            ...previous,
            [loanId]: { token, outcome: { kind: "failed", error } },
          }));
        });
    }

    return () => {
      controller.abort();
    };
    // `token` already encodes the id list and the refresh counter. Listing
    // `loanIds` itself would refetch on every render that rebuilt the array.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, token]);

  const submitDraft = useCallback(
    (event: React.FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      const candidate = draft.trim();
      if (!isLoanId(candidate)) {
        setDraftError(
          "That is not a loan id. Paste the UUID the API returned when the loan was created.",
        );
        return;
      }
      setDraftError(null);
      setDraft("");
      onTrack(candidate.toLowerCase());
    },
    [draft, onTrack],
  );

  return (
    <section aria-labelledby="accounts-heading">
      <h2 id="accounts-heading">Accounts</h2>
      <p className="view-intro">
        Loans this browser is following. The service has no list endpoint, so each loan is fetched
        by id and the balances below are always the service&apos;s own.
      </p>

      <form className="track-form" onSubmit={submitDraft}>
        <label htmlFor="loan-id-input">Follow a loan</label>
        <input
          id="loan-id-input"
          name="loanId"
          type="text"
          autoComplete="off"
          placeholder="00000000-0000-0000-0000-000000000000"
          value={draft}
          onChange={(event) => {
            setDraft(event.target.value);
          }}
        />
        <button type="submit">Add</button>
        {loanIds.length > 0 && (
          <button
            type="button"
            onClick={() => {
              setReloadToken((current) => current + 1);
            }}
          >
            Refresh balances
          </button>
        )}
      </form>

      {draftError !== null && (
        <p className="notice notice-error" role="alert">
          {draftError}
        </p>
      )}

      {loanIds.length === 0 ? (
        <Empty label="No loans followed yet. Create one against the API, then paste its id above." />
      ) : (
        <table className="data-table">
          <caption className="table-caption">Balances as the service reports them</caption>
          <thead>
            <tr>
              <th scope="col">Loan</th>
              <th scope="col">Status</th>
              <th scope="col">Total payable</th>
              <th scope="col">Paid</th>
              <th scope="col">Outstanding</th>
              <th scope="col">
                <span className="visually-hidden">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {loanIds.map((loanId) => {
              const entry = settled[loanId];
              const outcome: Outcome | null = entry?.token === token ? entry.outcome : null;

              return (
                <tr key={loanId}>
                  <th scope="row">
                    <code>{loanId}</code>
                  </th>
                  {outcome === null && (
                    <td colSpan={4}>
                      <Loading label="Loading this loan…" />
                    </td>
                  )}
                  {outcome?.kind === "failed" && (
                    <td colSpan={4}>
                      <ErrorNotice error={outcome.error} title="This loan could not be loaded" />
                    </td>
                  )}
                  {outcome?.kind === "loaded" && (
                    <>
                      <td>{outcome.loan.status}</td>
                      <td className="amount">{formatCents(outcome.loan.total_cents)}</td>
                      <td className="amount">{formatCents(outcome.loan.paid_cents)}</td>
                      <td className="amount">{formatCents(outcome.loan.outstanding_cents)}</td>
                    </>
                  )}
                  <td className="row-actions">
                    <button
                      type="button"
                      onClick={() => {
                        onOpenLedger(loanId);
                      }}
                    >
                      Ledger
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        onUntrack(loanId);
                      }}
                    >
                      Stop following
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

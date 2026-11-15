/**
 * The app shell: three views, one client, and the tracked loan set.
 *
 * No router, because three views do not need one and the task asks for no
 * libraries beyond what is needed.
 */

import { useCallback, useMemo, useState } from "react";
import type { JSX } from "react";

import { LedgerClient } from "./api/client";
import { AccountsView } from "./components/AccountsView";
import { LedgerView } from "./components/LedgerView";
import { PaymentForm } from "./components/PaymentForm";
import {
  addTrackedLoan,
  loadTrackedLoans,
  removeTrackedLoan,
  saveTrackedLoans,
} from "./lib/trackedLoans";

type ViewName = "accounts" | "ledger" | "payment";

const VIEW_LABELS: readonly { readonly name: ViewName; readonly label: string }[] = [
  { name: "accounts", label: "Accounts" },
  { name: "ledger", label: "Ledger" },
  { name: "payment", label: "Post a transaction" },
];

export interface AppProps {
  /** Injected in tests; the real app builds its own. */
  readonly client?: LedgerClient;
}

export function App({ client }: AppProps = {}): JSX.Element {
  const ledgerClient = useMemo(() => client ?? new LedgerClient(), [client]);

  const [view, setView] = useState<ViewName>("accounts");
  const [loanIds, setLoanIds] = useState<readonly string[]>(() => loadTrackedLoans());
  const [selectedLoanId, setSelectedLoanId] = useState<string | null>(null);
  const [ledgerReloadToken, setLedgerReloadToken] = useState(0);

  const persist = useCallback((next: readonly string[]) => {
    setLoanIds(next);
    saveTrackedLoans(next);
  }, []);

  const handleTrack = useCallback(
    (loanId: string) => {
      persist(addTrackedLoan(loanIds, loanId));
      setSelectedLoanId((current) => current ?? loanId);
    },
    [loanIds, persist],
  );

  const handleUntrack = useCallback(
    (loanId: string) => {
      persist(removeTrackedLoan(loanIds, loanId));
      setSelectedLoanId((current) => (current === loanId ? null : current));
    },
    [loanIds, persist],
  );

  const handleOpenLedger = useCallback((loanId: string) => {
    setSelectedLoanId(loanId);
    setView("ledger");
  }, []);

  return (
    <div className="app">
      <header className="app-header">
        <h1>Installment Ledger</h1>
        <p className="app-subtitle">
          Amounts are shown in dollars and sent to the API as integer cents.
        </p>
      </header>

      <nav aria-label="Views">
        <ul className="view-tabs">
          {VIEW_LABELS.map((entry) => (
            <li key={entry.name}>
              <button
                type="button"
                aria-current={view === entry.name ? "page" : undefined}
                onClick={() => {
                  setView(entry.name);
                }}
              >
                {entry.label}
              </button>
            </li>
          ))}
        </ul>
      </nav>

      <main>
        {view === "accounts" && (
          <AccountsView
            client={ledgerClient}
            loanIds={loanIds}
            onTrack={handleTrack}
            onUntrack={handleUntrack}
            onOpenLedger={handleOpenLedger}
          />
        )}
        {view === "ledger" && (
          <LedgerView
            client={ledgerClient}
            loanId={selectedLoanId}
            reloadToken={ledgerReloadToken}
          />
        )}
        {view === "payment" && (
          <PaymentForm
            client={ledgerClient}
            loanId={selectedLoanId}
            onPosted={() => {
              setLedgerReloadToken((token) => token + 1);
            }}
          />
        )}
      </main>
    </div>
  );
}

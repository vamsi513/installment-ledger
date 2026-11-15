/**
 * The set of loans the accounts view shows.
 *
 * The service has no list endpoint, and adding one would change the backend.
 * So the browser keeps the ids it has been shown and asks the service about
 * each one. The list is a local convenience, not a source of truth: the
 * balances on screen always come from `GET /loans/{id}`.
 *
 * Reads and writes are wrapped because storage can be unavailable or full in a
 * private window, and an unusable list must not take the page down with it.
 */

const STORAGE_KEY = "installment-ledger.tracked-loans";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** True when the string is shaped like a loan id the service would accept. */
export function isLoanId(value: string): boolean {
  return UUID_PATTERN.test(value.trim());
}

export function loadTrackedLoans(): string[] {
  let raw: string | null;
  try {
    raw = globalThis.localStorage.getItem(STORAGE_KEY);
  } catch {
    return [];
  }
  if (raw === null) {
    return [];
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw) as unknown;
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) {
    return [];
  }

  const seen = new Set<string>();
  for (const item of parsed) {
    if (typeof item === "string" && isLoanId(item)) {
      seen.add(item.trim().toLowerCase());
    }
  }
  return [...seen];
}

export function saveTrackedLoans(loanIds: readonly string[]): void {
  try {
    globalThis.localStorage.setItem(STORAGE_KEY, JSON.stringify(loanIds));
  } catch {
    // Nothing to do: the list is a convenience and the views still work.
  }
}

/** Add an id, keeping the list unique and leaving the order otherwise alone. */
export function addTrackedLoan(loanIds: readonly string[], loanId: string): string[] {
  const normalised = loanId.trim().toLowerCase();
  if (loanIds.includes(normalised)) {
    return [...loanIds];
  }
  return [...loanIds, normalised];
}

export function removeTrackedLoan(loanIds: readonly string[], loanId: string): string[] {
  const normalised = loanId.trim().toLowerCase();
  return loanIds.filter((item) => item !== normalised);
}

import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ApiError, TransportError } from "../api/errors";
import type { Ledger } from "../api/types";
import { LOAN_ID, deferred, makeLedger, stubClient } from "../test/fixtures";
import { LedgerView } from "./LedgerView";

describe("LedgerView", () => {
  it("prompts for a loan when none is selected", () => {
    const stub = stubClient();

    render(<LedgerView client={stub.client} loanId={null} />);

    expect(screen.getByText(/choose a loan/i)).toBeInTheDocument();
    expect(stub.getLedger).not.toHaveBeenCalled();
  });

  it("shows a loading state while the ledger is being fetched", async () => {
    const stub = stubClient();
    const pending = deferred<Ledger>();
    stub.getLedger.mockReturnValue(pending.promise);

    render(<LedgerView client={stub.client} loanId={LOAN_ID} />);

    expect(screen.getByRole("status")).toHaveTextContent(/loading the ledger/i);

    await act(async () => {
      pending.resolve(makeLedger());
      await pending.promise;
    });
  });

  it("renders the transactions and the totals the service reported", async () => {
    const stub = stubClient();
    stub.getLedger.mockResolvedValue(makeLedger());

    render(<LedgerView client={stub.client} loanId={LOAN_ID} />);

    expect(await screen.findByText("bbbbbbbb-0000-0000-0000-000000000001")).toBeInTheDocument();
    expect(screen.getByText("loan_receivable")).toBeInTheDocument();
    expect(screen.getByText("funding_payable")).toBeInTheDocument();
    expect(screen.getAllByText("$1,200.00").length).toBeGreaterThan(0);
    expect(screen.getAllByText("$1,500.00")).toHaveLength(2);
  });

  it("shows an empty state for a loan with no entries", async () => {
    const stub = stubClient();
    stub.getLedger.mockResolvedValue(
      makeLedger({ transactions: [], debit_cents: 0, credit_cents: 0, net_cents: 0 }),
    );

    render(<LedgerView client={stub.client} loanId={LOAN_ID} />);

    expect(await screen.findByText(/no ledger entries yet/i)).toBeInTheDocument();
  });

  it("shows a not found error with its code", async () => {
    const stub = stubClient();
    stub.getLedger.mockRejectedValue(new ApiError("loan_not_found", "No loan with that id.", 404));

    render(<LedgerView client={stub.client} loanId={LOAN_ID} />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The ledger could not be loaded");
    expect(alert).toHaveTextContent("No loan with that id.");
    expect(alert).toHaveTextContent("loan_not_found");
    expect(alert).toHaveTextContent("404");
  });

  it("shows a transport failure", async () => {
    const stub = stubClient();
    stub.getLedger.mockRejectedValue(new TransportError("Could not reach the ledger API."));

    render(<LedgerView client={stub.client} loanId={LOAN_ID} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not reach the ledger API.");
  });

  it("refetches when the reload token changes", async () => {
    const stub = stubClient();
    stub.getLedger.mockResolvedValue(makeLedger());

    const { rerender } = render(
      <LedgerView client={stub.client} loanId={LOAN_ID} reloadToken={0} />,
    );
    expect(await screen.findByText("bbbbbbbb-0000-0000-0000-000000000001")).toBeInTheDocument();
    expect(stub.getLedger).toHaveBeenCalledTimes(1);

    rerender(<LedgerView client={stub.client} loanId={LOAN_ID} reloadToken={1} />);
    expect(await screen.findByText("bbbbbbbb-0000-0000-0000-000000000001")).toBeInTheDocument();
    expect(stub.getLedger).toHaveBeenCalledTimes(2);
  });
});

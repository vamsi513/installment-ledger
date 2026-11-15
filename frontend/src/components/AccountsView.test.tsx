import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError, TransportError } from "../api/errors";
import type { Loan } from "../api/types";
import { LOAN_ID, deferred, makeLoan, stubClient } from "../test/fixtures";
import { AccountsView } from "./AccountsView";

const SECOND_LOAN_ID = "99999999-8888-7777-6666-555555555555";

function noop(): void {
  // Intentionally empty.
}

describe("AccountsView", () => {
  it("shows an empty state when no loan is followed", () => {
    const stub = stubClient();

    render(
      <AccountsView
        client={stub.client}
        loanIds={[]}
        onTrack={noop}
        onUntrack={noop}
        onOpenLedger={noop}
      />,
    );

    expect(screen.getByText(/no loans followed yet/i)).toBeInTheDocument();
    expect(stub.getLoan).not.toHaveBeenCalled();
  });

  it("shows a loading state per loan while the balances are fetched", async () => {
    const stub = stubClient();
    const pending = deferred<Loan>();
    stub.getLoan.mockReturnValue(pending.promise);

    render(
      <AccountsView
        client={stub.client}
        loanIds={[LOAN_ID]}
        onTrack={noop}
        onUntrack={noop}
        onOpenLedger={noop}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent(/loading this loan/i);

    await act(async () => {
      pending.resolve(makeLoan());
      await pending.promise;
    });
  });

  it("shows the balances the service reported, in dollars", async () => {
    const stub = stubClient();
    stub.getLoan.mockResolvedValue(makeLoan());

    render(
      <AccountsView
        client={stub.client}
        loanIds={[LOAN_ID]}
        onTrack={noop}
        onUntrack={noop}
        onOpenLedger={noop}
      />,
    );

    const row = await screen.findByRole("row", { name: new RegExp(LOAN_ID, "i") });
    expect(within(row).getByText("$1,200.00")).toBeInTheDocument();
    expect(within(row).getByText("$300.00")).toBeInTheDocument();
    expect(within(row).getByText("$900.00")).toBeInTheDocument();
    expect(within(row).getByText("open")).toBeInTheDocument();
  });

  it("fails one loan without taking the others down with it", async () => {
    const stub = stubClient();
    stub.getLoan.mockImplementation((loanId): Promise<Loan> => {
      if (loanId === LOAN_ID) {
        return Promise.resolve(makeLoan());
      }
      return Promise.reject(new ApiError("loan_not_found", "No loan with that id.", 404));
    });

    render(
      <AccountsView
        client={stub.client}
        loanIds={[LOAN_ID, SECOND_LOAN_ID]}
        onTrack={noop}
        onUntrack={noop}
        onOpenLedger={noop}
      />,
    );

    const goodRow = await screen.findByRole("row", { name: new RegExp(LOAN_ID, "i") });
    expect(within(goodRow).getByText("$900.00")).toBeInTheDocument();

    const badRow = await screen.findByRole("row", { name: new RegExp(SECOND_LOAN_ID, "i") });
    const alert = within(badRow).getByRole("alert");
    expect(alert).toHaveTextContent("This loan could not be loaded");
    expect(alert).toHaveTextContent("No loan with that id.");
    expect(alert).toHaveTextContent("loan_not_found");
  });

  it("shows a transport failure per loan", async () => {
    const stub = stubClient();
    stub.getLoan.mockRejectedValue(new TransportError("Could not reach the ledger API."));

    render(
      <AccountsView
        client={stub.client}
        loanIds={[LOAN_ID]}
        onTrack={noop}
        onUntrack={noop}
        onOpenLedger={noop}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not reach the ledger API.");
  });

  it("refuses something that is not a loan id and does not fetch it", async () => {
    const stub = stubClient();
    const onTrack = vi.fn();

    render(
      <AccountsView
        client={stub.client}
        loanIds={[]}
        onTrack={onTrack}
        onUntrack={noop}
        onOpenLedger={noop}
      />,
    );

    await userEvent.type(screen.getByLabelText("Follow a loan"), "not-a-uuid");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));

    expect(screen.getByRole("alert")).toHaveTextContent(/not a loan id/i);
    expect(onTrack).not.toHaveBeenCalled();
    expect(stub.getLoan).not.toHaveBeenCalled();
  });

  it("tracks a well formed loan id, normalised to lower case", async () => {
    const stub = stubClient();
    const onTrack = vi.fn();

    render(
      <AccountsView
        client={stub.client}
        loanIds={[]}
        onTrack={onTrack}
        onUntrack={noop}
        onOpenLedger={noop}
      />,
    );

    await userEvent.type(screen.getByLabelText("Follow a loan"), LOAN_ID.toUpperCase());
    await userEvent.click(screen.getByRole("button", { name: "Add" }));

    expect(onTrack).toHaveBeenCalledWith(LOAN_ID);
  });

  it("opens the ledger for a row and stops following on request", async () => {
    const stub = stubClient();
    stub.getLoan.mockResolvedValue(makeLoan());
    const onOpenLedger = vi.fn();
    const onUntrack = vi.fn();

    render(
      <AccountsView
        client={stub.client}
        loanIds={[LOAN_ID]}
        onTrack={noop}
        onUntrack={onUntrack}
        onOpenLedger={onOpenLedger}
      />,
    );

    const row = await screen.findByRole("row", { name: new RegExp(LOAN_ID, "i") });

    await userEvent.click(within(row).getByRole("button", { name: "Ledger" }));
    expect(onOpenLedger).toHaveBeenCalledWith(LOAN_ID);

    await userEvent.click(within(row).getByRole("button", { name: "Stop following" }));
    expect(onUntrack).toHaveBeenCalledWith(LOAN_ID);
  });

  it("refetches the balances when asked to refresh", async () => {
    const stub = stubClient();
    stub.getLoan.mockResolvedValue(makeLoan());

    render(
      <AccountsView
        client={stub.client}
        loanIds={[LOAN_ID]}
        onTrack={noop}
        onUntrack={noop}
        onOpenLedger={noop}
      />,
    );

    await screen.findByRole("row", { name: new RegExp(LOAN_ID, "i") });
    expect(stub.getLoan).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByRole("button", { name: "Refresh balances" }));

    await waitFor(() => {
      expect(stub.getLoan).toHaveBeenCalledTimes(2);
    });
  });
});

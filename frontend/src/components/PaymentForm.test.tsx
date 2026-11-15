import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError, TransportError } from "../api/errors";
import { LOAN_ID, deferred, makePayment, stubClient } from "../test/fixtures";
import type { Payment } from "../api/types";
import { PaymentForm } from "./PaymentForm";

function amountField(): HTMLElement {
  return screen.getByLabelText("Amount in dollars");
}

function submitButton(): HTMLElement {
  return screen.getByRole("button", { name: /post payment|posting/i });
}

describe("PaymentForm", () => {
  it("sends the amount as integer cents, not dollars", async () => {
    const stub = stubClient();
    stub.createPayment.mockResolvedValue(makePayment());

    render(<PaymentForm client={stub.client} loanId={LOAN_ID} />);

    await userEvent.type(amountField(), "125.45");
    await userEvent.click(submitButton());

    await waitFor(() => {
      expect(stub.createPayment).toHaveBeenCalledTimes(1);
    });
    expect(stub.createPayment).toHaveBeenCalledWith(
      LOAN_ID,
      { amount_cents: 12_545 },
      expect.any(String),
    );
  });

  it("sends a whole dollar amount as cents", async () => {
    const stub = stubClient();
    stub.createPayment.mockResolvedValue(makePayment());

    render(<PaymentForm client={stub.client} loanId={LOAN_ID} />);

    await userEvent.type(amountField(), "300");
    await userEvent.click(submitButton());

    await waitFor(() => {
      expect(stub.createPayment).toHaveBeenCalledWith(
        LOAN_ID,
        { amount_cents: 30_000 },
        expect.any(String),
      );
    });
  });

  it("generates a fresh idempotency key for each submitted attempt", async () => {
    const stub = stubClient();
    stub.createPayment.mockResolvedValue(makePayment());

    render(<PaymentForm client={stub.client} loanId={LOAN_ID} />);

    await userEvent.type(amountField(), "10.00");
    await userEvent.click(submitButton());
    await waitFor(() => {
      expect(stub.createPayment).toHaveBeenCalledTimes(1);
    });

    await userEvent.type(amountField(), "10.00");
    await userEvent.click(submitButton());
    await waitFor(() => {
      expect(stub.createPayment).toHaveBeenCalledTimes(2);
    });

    const firstKey = stub.createPayment.mock.calls[0]?.[2] as string;
    const secondKey = stub.createPayment.mock.calls[1]?.[2] as string;
    expect(firstKey).toBeTruthy();
    expect(secondKey).toBeTruthy();
    expect(secondKey).not.toBe(firstKey);
  });

  it("cannot double post when the submit is repeated before the first finishes", async () => {
    const stub = stubClient();
    const pending = deferred<Payment>();
    stub.createPayment.mockReturnValue(pending.promise);

    render(<PaymentForm client={stub.client} loanId={LOAN_ID} />);
    await userEvent.type(amountField(), "50.00");

    const form = submitButton().closest("form");
    expect(form).not.toBeNull();

    // Two submits in the same tick, with no render in between. This is the
    // case the disabled attribute alone would not catch.
    act(() => {
      form?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      form?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });

    expect(stub.createPayment).toHaveBeenCalledTimes(1);

    await act(async () => {
      pending.resolve(makePayment());
      await pending.promise;
    });

    expect(stub.createPayment).toHaveBeenCalledTimes(1);
  });

  it("disables the button while a payment is in flight", async () => {
    const stub = stubClient();
    const pending = deferred<Payment>();
    stub.createPayment.mockReturnValue(pending.promise);

    render(<PaymentForm client={stub.client} loanId={LOAN_ID} />);
    await userEvent.type(amountField(), "50.00");
    await userEvent.click(submitButton());

    await waitFor(() => {
      expect(submitButton()).toBeDisabled();
    });

    // A further click on the disabled control posts nothing.
    await userEvent.click(submitButton());
    expect(stub.createPayment).toHaveBeenCalledTimes(1);

    await act(async () => {
      pending.resolve(makePayment());
      await pending.promise;
    });

    await waitFor(() => {
      expect(submitButton()).not.toBeDisabled();
    });
  });

  it("refuses an amount with more than two decimal places without calling the API", async () => {
    const stub = stubClient();

    render(<PaymentForm client={stub.client} loanId={LOAN_ID} />);

    await userEvent.type(amountField(), "1.234");
    await userEvent.click(submitButton());

    expect(await screen.findByRole("alert")).toHaveTextContent(/at most two decimal places/i);
    expect(stub.createPayment).not.toHaveBeenCalled();
  });

  it("refuses an empty amount without calling the API", async () => {
    const stub = stubClient();

    render(<PaymentForm client={stub.client} loanId={LOAN_ID} />);
    await userEvent.click(submitButton());

    expect(await screen.findByRole("alert")).toHaveTextContent(/enter an amount/i);
    expect(stub.createPayment).not.toHaveBeenCalled();
  });

  it("refuses a zero amount, which the API would reject anyway", async () => {
    const stub = stubClient();

    render(<PaymentForm client={stub.client} loanId={LOAN_ID} />);
    await userEvent.type(amountField(), "0.00");
    await userEvent.click(submitButton());

    expect(await screen.findByRole("alert")).toHaveTextContent(/at least one cent/i);
    expect(stub.createPayment).not.toHaveBeenCalled();
  });

  it("shows the service's validation error with its field detail", async () => {
    const stub = stubClient();
    stub.createPayment.mockRejectedValue(
      new ApiError("validation_error", "The request body or path is not valid.", 422, {
        fields: [
          {
            location: ["body", "amount_cents"],
            message: "Input should be greater than or equal to 1",
            type: "greater_than_equal",
          },
        ],
      }),
    );

    render(<PaymentForm client={stub.client} loanId={LOAN_ID} />);
    await userEvent.type(amountField(), "5.00");
    await userEvent.click(submitButton());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The request body or path is not valid.");
    expect(alert).toHaveTextContent("validation_error");
    expect(alert).toHaveTextContent("body.amount_cents");
    expect(alert).toHaveTextContent("Input should be greater than or equal to 1");
  });

  it("shows a duplicate request plainly, including that nothing posted twice", async () => {
    const stub = stubClient();
    stub.createPayment.mockRejectedValue(
      new ApiError(
        "idempotency_key_conflict",
        "This key was already used with a different request body.",
        409,
      ),
    );

    render(<PaymentForm client={stub.client} loanId={LOAN_ID} />);
    await userEvent.type(amountField(), "5.00");
    await userEvent.click(submitButton());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("This key was already used with a different request body.");
    expect(alert).toHaveTextContent("idempotency_key_conflict");
    expect(alert).toHaveTextContent(/nothing was posted twice/i);
  });

  it("shows an in flight duplicate as a duplicate too", async () => {
    const stub = stubClient();
    stub.createPayment.mockRejectedValue(
      new ApiError("idempotency_request_in_flight", "An identical request is still running.", 409),
    );

    render(<PaymentForm client={stub.client} loanId={LOAN_ID} />);
    await userEvent.type(amountField(), "5.00");
    await userEvent.click(submitButton());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("An identical request is still running.");
    expect(alert).toHaveTextContent(/nothing was posted twice/i);
  });

  it("shows an overpayment refusal with its code", async () => {
    const stub = stubClient();
    stub.createPayment.mockRejectedValue(
      new ApiError("overpayment", "That payment would overpay the loan.", 422),
    );

    render(<PaymentForm client={stub.client} loanId={LOAN_ID} />);
    await userEvent.type(amountField(), "5000.00");
    await userEvent.click(submitButton());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("That payment would overpay the loan.");
    expect(alert).toHaveTextContent("overpayment");
    expect(alert).not.toHaveTextContent(/nothing was posted twice/i);
  });

  it("shows a transport failure when the API cannot be reached", async () => {
    const stub = stubClient();
    stub.createPayment.mockRejectedValue(new TransportError("Could not reach the ledger API."));

    render(<PaymentForm client={stub.client} loanId={LOAN_ID} />);
    await userEvent.type(amountField(), "5.00");
    await userEvent.click(submitButton());

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not reach the ledger API.");
  });

  it("reports the posted payment in dollars and clears the field", async () => {
    const stub = stubClient();
    stub.createPayment.mockResolvedValue(makePayment());

    render(<PaymentForm client={stub.client} loanId={LOAN_ID} />);
    await userEvent.type(amountField(), "125.00");
    await userEvent.click(submitButton());

    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("$125.00");
    expect(status).toHaveTextContent("$775.00");
    expect(amountField()).toHaveValue("");
  });

  it("will not submit at all without a selected loan", () => {
    const stub = stubClient();

    render(<PaymentForm client={stub.client} loanId={null} />);

    expect(submitButton()).toBeDisabled();
    expect(screen.getByText("No loan selected")).toBeInTheDocument();
    expect(stub.createPayment).not.toHaveBeenCalled();
  });
});

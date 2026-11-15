import { describe, expect, it } from "vitest";

import { API_ERROR_CODES, ApiError, describeError, parseErrorBody } from "./errors";

describe("API_ERROR_CODES", () => {
  it("matches the codes the service documents", () => {
    // app/errors.py and the README endpoint table. If the backend adds a code,
    // this is the line that should fail.
    expect([...API_ERROR_CODES]).toEqual([
      "validation_error",
      "loan_not_found",
      "loan_closed",
      "overpayment",
      "missing_idempotency_key",
      "idempotency_key_conflict",
      "idempotency_request_in_flight",
    ]);
  });
});

describe("parseErrorBody", () => {
  it("reads the service's envelope", () => {
    const error = parseErrorBody(422, {
      error: { code: "overpayment", message: "That would overpay the loan.", details: { a: 1 } },
    });

    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe("overpayment");
    expect(error.message).toBe("That would overpay the loan.");
    expect(error.status).toBe(422);
    expect(error.details).toEqual({ a: 1 });
  });

  it("flags the two duplicate request codes and nothing else", () => {
    const conflict = parseErrorBody(409, {
      error: { code: "idempotency_key_conflict", message: "Reused with a different body." },
    });
    const inFlight = parseErrorBody(409, {
      error: { code: "idempotency_request_in_flight", message: "Still running." },
    });
    const overpayment = parseErrorBody(422, {
      error: { code: "overpayment", message: "Too much." },
    });

    expect(conflict.isDuplicateRequest).toBe(true);
    expect(inFlight.isDuplicateRequest).toBe(true);
    expect(overpayment.isDuplicateRequest).toBe(false);
  });

  it("exposes the per field detail of a validation error", () => {
    const error = parseErrorBody(422, {
      error: {
        code: "validation_error",
        message: "The request body or path is not valid.",
        details: {
          fields: [
            {
              location: ["body", "amount_cents"],
              message: "Input should be greater than or equal to 1",
              type: "greater_than_equal",
            },
          ],
        },
      },
    });

    expect(error.validationFields).toHaveLength(1);
    expect(error.validationFields[0]?.location).toEqual(["body", "amount_cents"]);
    expect(error.validationFields[0]?.message).toBe("Input should be greater than or equal to 1");
  });

  it("returns no fields when the service sent none", () => {
    const error = parseErrorBody(404, {
      error: { code: "loan_not_found", message: "No such loan." },
    });
    expect(error.validationFields).toEqual([]);
  });

  it("ignores malformed entries in the field list", () => {
    const error = parseErrorBody(422, {
      error: {
        code: "validation_error",
        message: "Bad.",
        details: { fields: [null, 7, { message: "ok", location: ["body"], type: "x" }] },
      },
    });
    expect(error.validationFields).toHaveLength(1);
  });

  it("still produces an error for a body that is not the envelope", () => {
    const error = parseErrorBody(502, "<html>Bad gateway</html>");
    expect(error.code).toBe("unknown_error");
    expect(error.status).toBe(502);
    expect(error.message).toContain("502");
  });

  it("falls back to a status message when the envelope has no message", () => {
    const error = parseErrorBody(500, { error: { code: "internal_error" } });
    expect(error.code).toBe("internal_error");
    expect(error.message).toContain("500");
  });
});

describe("describeError", () => {
  it("uses the service's message when there is one", () => {
    expect(describeError(new ApiError("loan_closed", "This loan is closed.", 422))).toBe(
      "This loan is closed.",
    );
  });

  it("handles something that is not an error at all", () => {
    expect(describeError("nope")).toBe("Something went wrong.");
  });
});

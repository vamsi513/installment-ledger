/**
 * The API's single error envelope, read into something the UI can branch on.
 *
 * Every failure from the service, validation included, arrives as
 * `{"error": {"code", "message", "details"}}` (app/main.py, app/errors.py).
 * The `code` is documented as stable and safe to branch on, so the UI branches
 * on it and shows `message` to the reader.
 */

/**
 * The codes the README documents. An undocumented code is still shown to the
 * reader rather than swallowed; it simply does not match a branch here.
 */
export const API_ERROR_CODES = [
  "validation_error",
  "loan_not_found",
  "loan_closed",
  "overpayment",
  "missing_idempotency_key",
  "idempotency_key_conflict",
  "idempotency_request_in_flight",
] as const;

export type ApiErrorCode = (typeof API_ERROR_CODES)[number];

/** The codes that mean "this request was already seen", not "this request is wrong". */
export const DUPLICATE_REQUEST_CODES: readonly ApiErrorCode[] = [
  "idempotency_key_conflict",
  "idempotency_request_in_flight",
];

/** One entry of `details.fields` on a `validation_error` (app/main.py). */
export interface ValidationField {
  readonly location: readonly (string | number)[];
  readonly message: string;
  readonly type: string;
}

/** A failure the service described in its own envelope. */
export class ApiError extends Error {
  public readonly code: string;
  public readonly status: number;
  public readonly details: Readonly<Record<string, unknown>>;

  public constructor(
    code: string,
    message: string,
    status: number,
    details: Readonly<Record<string, unknown>> = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.details = details;
  }

  /** True when the service refused because it had seen this key before. */
  public get isDuplicateRequest(): boolean {
    return (DUPLICATE_REQUEST_CODES as readonly string[]).includes(this.code);
  }

  /** The per field validation detail, when the service sent any. */
  public get validationFields(): readonly ValidationField[] {
    const fields = this.details["fields"];
    if (!Array.isArray(fields)) {
      return [];
    }
    return fields.filter((field): field is ValidationField => {
      if (typeof field !== "object" || field === null) {
        return false;
      }
      const candidate = field as Partial<ValidationField>;
      return typeof candidate.message === "string" && Array.isArray(candidate.location);
    });
  }
}

/** The request never reached the service, or its answer was unreadable. */
export class TransportError extends Error {
  public constructor(message: string) {
    super(message);
    this.name = "TransportError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Read an error response body into an `ApiError`.
 *
 * A body that does not match the envelope still produces an `ApiError`, so a
 * proxy error page or an empty 502 reaches the reader as a message rather than
 * as a blank screen.
 */
export function parseErrorBody(status: number, body: unknown): ApiError {
  if (isRecord(body) && isRecord(body["error"])) {
    const envelope = body["error"];
    const code = typeof envelope["code"] === "string" ? envelope["code"] : "unknown_error";
    const message =
      typeof envelope["message"] === "string" && envelope["message"] !== ""
        ? envelope["message"]
        : `The service returned status ${String(status)}.`;
    const details = isRecord(envelope["details"]) ? envelope["details"] : {};
    return new ApiError(code, message, status, details);
  }

  return new ApiError(
    "unknown_error",
    `The service returned status ${String(status)} and an unrecognised body.`,
    status,
  );
}

/** A sentence for the reader, whatever went wrong. */
export function describeError(error: unknown): string {
  if (error instanceof ApiError || error instanceof TransportError) {
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "Something went wrong.";
}

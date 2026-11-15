/**
 * Money handling for the browser.
 *
 * The API transports every amount as an integer number of cents, and so does
 * this module. No amount is ever held, parsed or rendered as a fractional
 * number: the two conversions below go between an integer cent count and a
 * decimal string, and both do it with integer arithmetic and string work only.
 *
 * The backend caps amounts at 100_000_000 cents, so every value this module
 * sees fits comfortably inside a safe integer.
 */

export const CENTS_PER_DOLLAR = 100;

/** A dollar string that could not be read as a whole number of cents. */
export class MoneyFormatError extends Error {
  public constructor(message: string) {
    super(message);
    this.name = "MoneyFormatError";
  }
}

function assertIntegerCents(cents: number): void {
  if (!Number.isSafeInteger(cents)) {
    throw new MoneyFormatError(
      `Amounts must be a whole number of cents, received ${String(cents)}.`,
    );
  }
}

/** Insert a comma every three digits, counting from the right. */
function groupThousands(digits: string): string {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/**
 * Split an integer cent count into whole dollars and the remaining cents.
 *
 * `(abs - remainder)` is an exact multiple of 100, and dividing it by 100
 * yields an integer that is itself exactly representable, so IEEE 754 returns
 * that integer rather than an approximation of it. This is the only division
 * in the module.
 */
function splitCents(cents: number): { negative: boolean; dollars: number; remainder: number } {
  assertIntegerCents(cents);
  const negative = cents < 0;
  const abs = negative ? -cents : cents;
  const remainder = abs % CENTS_PER_DOLLAR;
  return { negative, dollars: (abs - remainder) / CENTS_PER_DOLLAR, remainder };
}

/** Render cents as a plain decimal string, for example `1234.56`. */
export function formatCentsPlain(cents: number): string {
  const { negative, dollars, remainder } = splitCents(cents);
  const fraction = remainder.toString().padStart(2, "0");
  return `${negative ? "-" : ""}${dollars.toString()}.${fraction}`;
}

/** Render cents for display, for example `$1,234.56`. */
export function formatCents(cents: number): string {
  const { negative, dollars, remainder } = splitCents(cents);
  const fraction = remainder.toString().padStart(2, "0");
  return `${negative ? "-" : ""}$${groupThousands(dollars.toString())}.${fraction}`;
}

// An optional sign, an optional dollar sign, digits that may be grouped by
// commas, and at most two decimal places. Anything else is a rejection rather
// than a guess, because guessing at an amount is the wrong thing to do.
const DOLLAR_INPUT = /^([+-])?\s*\$?\s*(\d+|\d{1,3}(?:,\d{3})+)(?:\.(\d{1,2}))?$/;

/**
 * Read a dollar string as an integer number of cents.
 *
 * The digits are concatenated into one integer string and parsed once, so the
 * result never passes through a fractional value.
 */
export function parseDollarsToCents(raw: string): number {
  const trimmed = raw.trim();
  if (trimmed === "") {
    throw new MoneyFormatError("Enter an amount.");
  }

  const match = DOLLAR_INPUT.exec(trimmed);
  if (match === null) {
    throw new MoneyFormatError(
      "Enter an amount in dollars with at most two decimal places, for example 125.00.",
    );
  }

  const sign = match[1] ?? "+";
  const whole = (match[2] ?? "").replace(/,/g, "");
  const fraction = (match[3] ?? "").padEnd(2, "0");

  const magnitude = Number(`${whole}${fraction}`);
  if (!Number.isSafeInteger(magnitude)) {
    throw new MoneyFormatError("That amount is too large.");
  }

  return sign === "-" ? -magnitude : magnitude;
}

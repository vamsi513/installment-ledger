import { describe, expect, it } from "vitest";

import {
  MoneyFormatError,
  formatCents,
  formatCentsPlain,
  parseDollarsToCents,
} from "./money";

describe("formatCents", () => {
  it("renders whole dollars with two decimal places", () => {
    expect(formatCents(120_000)).toBe("$1,200.00");
  });

  it("pads a single cent remainder", () => {
    expect(formatCents(1)).toBe("$0.01");
    expect(formatCents(7)).toBe("$0.07");
    expect(formatCents(10)).toBe("$0.10");
  });

  it("groups thousands", () => {
    expect(formatCents(100_000_000)).toBe("$1,000,000.00");
    expect(formatCents(99_999)).toBe("$999.99");
    expect(formatCents(1_000_00)).toBe("$1,000.00");
  });

  it("renders a negative amount with the sign before the dollar mark", () => {
    expect(formatCents(-2_599)).toBe("-$25.99");
  });

  it("renders zero", () => {
    expect(formatCents(0)).toBe("$0.00");
  });

  it("refuses an amount that is not a whole number of cents", () => {
    expect(() => formatCents(12.5)).toThrow(MoneyFormatError);
    expect(() => formatCents(Number.NaN)).toThrow(MoneyFormatError);
    expect(() => formatCents(Number.POSITIVE_INFINITY)).toThrow(MoneyFormatError);
  });

  it("is exact for every cent remainder, with no rounding drift", () => {
    for (let remainder = 0; remainder < 100; remainder += 1) {
      const cents = 123_400 + remainder;
      const expected = `$1,234.${remainder.toString().padStart(2, "0")}`;
      expect(formatCents(cents)).toBe(expected);
    }
  });

  it("is exact at amounts where float cent arithmetic would drift", () => {
    // 0.1 + 0.2 and 1.005 style cases: these are the amounts that go wrong
    // when money is held as a fraction of a dollar.
    expect(formatCents(30)).toBe("$0.30");
    expect(formatCents(1_005)).toBe("$10.05");
    expect(formatCents(8_430_000_07)).toBe("$8,430,000.07");
  });
});

describe("formatCentsPlain", () => {
  it("renders without a currency mark or grouping, for form fields", () => {
    expect(formatCentsPlain(123_456)).toBe("1234.56");
    expect(formatCentsPlain(5)).toBe("0.05");
    expect(formatCentsPlain(-5)).toBe("-0.05");
  });
});

describe("parseDollarsToCents", () => {
  it("reads whole dollars", () => {
    expect(parseDollarsToCents("125")).toBe(12_500);
  });

  it("reads two decimal places", () => {
    expect(parseDollarsToCents("125.00")).toBe(12_500);
    expect(parseDollarsToCents("125.45")).toBe(12_545);
  });

  it("reads one decimal place as tens of cents", () => {
    expect(parseDollarsToCents("125.4")).toBe(12_540);
  });

  it("accepts a dollar mark, grouping commas and surrounding space", () => {
    expect(parseDollarsToCents("  $1,234.56 ")).toBe(123_456);
    expect(parseDollarsToCents("$0.01")).toBe(1);
  });

  it("accepts a sign", () => {
    expect(parseDollarsToCents("-25.99")).toBe(-2_599);
    expect(parseDollarsToCents("+25.99")).toBe(2_599);
  });

  it("refuses more than two decimal places rather than rounding", () => {
    expect(() => parseDollarsToCents("1.234")).toThrow(MoneyFormatError);
    expect(() => parseDollarsToCents("0.005")).toThrow(MoneyFormatError);
  });

  it("refuses an empty or non numeric amount", () => {
    expect(() => parseDollarsToCents("")).toThrow(MoneyFormatError);
    expect(() => parseDollarsToCents("   ")).toThrow(MoneyFormatError);
    expect(() => parseDollarsToCents("abc")).toThrow(MoneyFormatError);
    expect(() => parseDollarsToCents("12.3.4")).toThrow(MoneyFormatError);
    expect(() => parseDollarsToCents("1e5")).toThrow(MoneyFormatError);
    expect(() => parseDollarsToCents("12,34")).toThrow(MoneyFormatError);
  });

  it("round trips every cent remainder through format and parse", () => {
    for (let remainder = 0; remainder < 100; remainder += 1) {
      const cents = 7_600 + remainder;
      expect(parseDollarsToCents(formatCentsPlain(cents))).toBe(cents);
      expect(parseDollarsToCents(formatCents(cents))).toBe(cents);
    }
  });

  it("always returns a safe integer", () => {
    for (const input of ["0.01", "1", "1.1", "999,999.99", "1,000,000.00"]) {
      expect(Number.isSafeInteger(parseDollarsToCents(input))).toBe(true);
    }
  });
});

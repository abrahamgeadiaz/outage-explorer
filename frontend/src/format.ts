import type { Cell } from "./api/types";

const integer = new Intl.NumberFormat("en-US");
const decimal = new Intl.NumberFormat("en-US", { maximumFractionDigits: 3 });

export function formatNumber(n: number): string {
  return Number.isInteger(n) ? integer.format(n) : decimal.format(n);
}

export function formatCell(value: Cell): string {
  if (value === null) return "";
  if (typeof value === "number") return formatNumber(value);
  if (typeof value === "boolean") return value ? "true" : "false";
  if (Array.isArray(value)) return value.map(formatCell).join(", ");
  return value;
}

export function isNumericType(type: string): boolean {
  return /^(DECIMAL|DOUBLE|FLOAT|REAL|INTEGER|BIGINT|SMALLINT|TINYINT|HUGEINT|UBIGINT|UINTEGER)/i.test(type);
}

import type { Cell, Column } from "./api/types";

// Spreadsheets run a text cell that starts with one of these as a formula (CSV injection), and the text
// can come from a user's SQL (`SELECT '=HYPERLINK(...)'`). A leading apostrophe keeps it plain text.
// Only strings: a negative number stays a number.
const FORMULA_START = /^[=+\-@\t\r]/;

function field(value: Cell): string {
  if (value === null) return "";
  let text = Array.isArray(value) ? JSON.stringify(value) : String(value);
  if (typeof value === "string" && FORMULA_START.test(text)) text = `'${text}`;
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

export function toCsv(columns: Column[], rows: Cell[][]): string {
  return [columns.map((c) => field(c.name)), ...rows.map((r) => r.map(field))].map((r) => r.join(",")).join("\r\n");
}

export function downloadCsv(filename: string, csv: string): void {
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

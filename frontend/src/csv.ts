import type { Cell, Column } from "./api/types";

function field(value: Cell): string {
  if (value === null) return "";
  const text = Array.isArray(value) ? JSON.stringify(value) : String(value);
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

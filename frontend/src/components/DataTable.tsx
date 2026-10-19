import type { Cell, Column } from "../api/types";
import { formatCell, isNumericType } from "../format";

interface Props {
  columns: Column[];
  rows: Cell[][];
  caption: string;
  busy?: boolean; // a new page is loading; the old one stays visible, dimmed
}

export function DataTable({ columns, rows, caption, busy }: Props) {
  return (
    <div className={`table-wrap${busy ? " busy" : ""}`} aria-busy={busy || undefined}>
      <table>
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.name} scope="col" title={c.description ? `${c.type}: ${c.description}` : c.type}>
                {c.name}
                <span className="col-type">{c.type}</span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {row.map((value, j) => (
                <td key={j} className={isNumericType(columns[j]?.type ?? "") ? "num" : undefined}>
                  {value === null ? <span className="null">null</span> : formatCell(value)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

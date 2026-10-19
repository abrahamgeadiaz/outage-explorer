import { formatNumber } from "../format";

export const PAGE_SIZES = [50, 100, 500, 1000];

interface Props {
  offset: number;
  limit: number;
  total: number;
  shown: number;
  onChange(offset: number, limit: number): void;
}

// Paging is done by the backend: the table only ever holds one page.
export function Pager({ offset, limit, total, shown, onChange }: Props) {
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.max(1, Math.ceil(total / limit));
  const lastOffset = (pages - 1) * limit;
  return (
    <nav className="pager" aria-label="Pages">
      <span>
        {total === 0
          ? "No rows"
          : `Rows ${formatNumber(offset + 1)}–${formatNumber(offset + shown)} of ${formatNumber(total)} · page ${formatNumber(page)} of ${formatNumber(pages)}`}
      </span>
      <span className="pager-buttons">
        <button type="button" onClick={() => onChange(0, limit)} disabled={offset === 0} aria-label="First page">
          «
        </button>
        <button
          type="button"
          onClick={() => onChange(Math.max(0, offset - limit), limit)}
          disabled={offset === 0}
          aria-label="Previous page"
        >
          ‹ Prev
        </button>
        <button
          type="button"
          onClick={() => onChange(offset + limit, limit)}
          disabled={offset + limit >= total}
          aria-label="Next page"
        >
          Next ›
        </button>
        <button
          type="button"
          onClick={() => onChange(lastOffset, limit)}
          disabled={offset >= lastOffset}
          aria-label="Last page"
        >
          »
        </button>
        <label>
          <span className="sr-only">Rows per page</span>
          <select value={limit} onChange={(e) => onChange(0, Number(e.target.value))}>
            {PAGE_SIZES.map((n) => (
              <option key={n} value={n}>
                {n} / page
              </option>
            ))}
          </select>
        </label>
      </span>
    </nav>
  );
}

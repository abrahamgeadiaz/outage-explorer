// Query history, per user, in this browser only (a convenience; nothing depends on it surviving).

const MAX_ENTRIES = 20;

export interface HistoryEntry {
  sql: string;
  at: string; // ISO time of the last run
  ok: boolean;
}

const key = (username: string) => `outage-explorer.history.${username}`;

export function loadHistory(username: string): HistoryEntry[] {
  try {
    const raw = localStorage.getItem(key(username));
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.slice(0, MAX_ENTRIES) : [];
  } catch {
    return [];
  }
}

export function remember(username: string, sql: string, ok: boolean): HistoryEntry[] {
  const entries = [
    { sql, at: new Date().toISOString(), ok },
    ...loadHistory(username).filter((e) => e.sql.trim() !== sql.trim()),
  ].slice(0, MAX_ENTRIES);
  try {
    localStorage.setItem(key(username), JSON.stringify(entries));
  } catch {
    /* storage unavailable: history lasts for this page only */
  }
  return entries;
}

export function bytes(n: number | undefined): string {
  let f = n || 0;
  for (const u of ['B', 'KB', 'MB', 'GB', 'TB']) {
    if (f < 1024 || u === 'TB') return u === 'B' ? `${f} B` : `${f.toFixed(1)} ${u}`;
    f /= 1024;
  }
  return '';
}

export function duration(s: number | undefined): string {
  if (s === undefined || !isFinite(s)) return '';
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h) return `${h} h ${m} min`;
  if (m) return `${m} min ${s % 60} s`;
  return `${s} s`;
}

export function pct(done: number, size: number): number {
  return size > 0 ? Math.min(100, Math.round((done / size) * 100)) : 0;
}

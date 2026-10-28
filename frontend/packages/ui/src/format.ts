/** Time and number formatting. Every time is shown in the household's time zone
 *  (the session says which; Asia/Kolkata by default), the way people say it:
 *  "6:22 pm", "Yesterday, 11:05 pm", "Tue 27 Oct, 6:22 pm". */

export const DEFAULT_TZ = 'Asia/Kolkata';

function parts(date: Date, tz: string, opts: Intl.DateTimeFormatOptions) {
  return new Intl.DateTimeFormat('en-IN', { timeZone: tz, ...opts }).formatToParts(date);
}

function toDate(value: string | number | Date): Date {
  return value instanceof Date ? value : new Date(value);
}

/** "6:22 pm" */
export function formatTime(value: string | number | Date, tz = DEFAULT_TZ): string {
  const p = parts(toDate(value), tz, { hour: 'numeric', minute: '2-digit', hour12: true });
  const hour = p.find((x) => x.type === 'hour')?.value ?? '';
  const minute = p.find((x) => x.type === 'minute')?.value ?? '';
  const period = (p.find((x) => x.type === 'dayPeriod')?.value ?? '').toLowerCase().replace(/\./g, '');
  return `${hour}:${minute} ${period}`.trim();
}

/** Calendar day key in the time zone, e.g. "2026-10-28". */
export function dayKey(value: string | number | Date, tz = DEFAULT_TZ): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' }).format(toDate(value));
}

/** "Today", "Yesterday" or "Tue 27 Oct". */
export function dayLabel(value: string | number | Date, tz = DEFAULT_TZ, now: Date = new Date()): string {
  const d = toDate(value);
  const today = dayKey(now, tz);
  const yesterday = dayKey(new Date(now.getTime() - 86_400_000), tz);
  const key = dayKey(d, tz);
  if (key === today) return 'Today';
  if (key === yesterday) return 'Yesterday';
  const p = parts(d, tz, { weekday: 'short', day: 'numeric', month: 'short' });
  const wd = p.find((x) => x.type === 'weekday')?.value ?? '';
  const day = p.find((x) => x.type === 'day')?.value ?? '';
  const month = p.find((x) => x.type === 'month')?.value ?? '';
  return `${wd} ${day} ${month}`;
}

/** "Today, 6:22 pm" style. */
export function formatDayTime(value: string | number | Date, tz = DEFAULT_TZ, now: Date = new Date()): string {
  return `${dayLabel(value, tz, now)}, ${formatTime(value, tz)}`;
}

/** "28:41" or "1:02:05" for a countdown in milliseconds (never negative). */
export function formatCountdown(ms: number): string {
  const total = Math.max(0, Math.ceil(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const ss = String(s).padStart(2, '0');
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

/** "3 min", "1 h 5 min" for a duration in seconds. */
export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  const rest = m % 60;
  return rest ? `${h} h ${rest} min` : `${h} h`;
}

/** "86.2 GB" */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes)) return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = bytes;
  let i = 0;
  while (v >= 1000 && i < units.length - 1) {
    v /= 1000;
    i += 1;
  }
  return `${v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

export function capitalise(text: string): string {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

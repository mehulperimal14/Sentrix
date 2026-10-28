import { describe, expect, it } from 'vitest';
import { dayLabel, formatBytes, formatCountdown, formatDuration, formatTime } from './format';

describe('format', () => {
  it('shows times in the home time zone the way people say them', () => {
    // 12:52 UTC is 6:22 pm in India
    expect(formatTime('2026-10-28T12:52:00Z', 'Asia/Kolkata')).toBe('6:22 pm');
    expect(formatTime('2026-10-28T03:35:00Z', 'Asia/Kolkata')).toBe('9:05 am');
  });

  it('names days relative to now', () => {
    const now = new Date('2026-10-28T12:00:00Z');
    expect(dayLabel('2026-10-28T05:00:00Z', 'Asia/Kolkata', now)).toBe('Today');
    expect(dayLabel('2026-10-27T05:00:00Z', 'Asia/Kolkata', now)).toBe('Yesterday');
    expect(dayLabel('2026-10-26T05:00:00Z', 'Asia/Kolkata', now)).toBe('Mon 26 Oct');
  });

  it('formats countdowns and durations', () => {
    expect(formatCountdown(1_721_000)).toBe('28:41');
    expect(formatCountdown(-5)).toBe('0:00');
    expect(formatCountdown(3_725_000)).toBe('1:02:05');
    expect(formatDuration(30)).toBe('30 s');
    expect(formatDuration(180)).toBe('3 min');
    expect(formatDuration(3900)).toBe('1 h 5 min');
    expect(formatBytes(86_190_000_000)).toBe('86 GB');
  });
});

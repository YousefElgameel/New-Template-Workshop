import { afterEach, describe, expect, it, vi } from 'vitest';
import { formatRelativeTime } from '@/lib/relative-time';

// "Now" is Tuesday 6 Oct 2026, 15:00 UTC. Every case names its time zone, so the result does not
// depend on the machine the tests run on.
const NOW = new Date('2026-10-06T15:00:00.000Z');
const fmt = (iso: string, tz = 'UTC', now = NOW) => formatRelativeTime(iso, now, tz);

describe('formatRelativeTime', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('says "today HH:MM" for the same calendar day, 24-hour, zero-padded', () => {
    expect(fmt('2026-10-06T10:12:03.400Z')).toBe('today 10:12');
    expect(fmt('2026-10-06T09:05:00.000Z')).toBe('today 09:05');
    expect(fmt('2026-10-06T00:00:00.000Z')).toBe('today 00:00');
    expect(fmt('2026-10-06T23:59:59.999Z', 'UTC', new Date('2026-10-06T23:59:59.999Z'))).toBe(
      'today 23:59',
    );
  });

  it('says "yesterday" for the previous calendar day, whatever the hour', () => {
    expect(fmt('2026-10-05T16:30:00.000Z')).toBe('yesterday');
    expect(fmt('2026-10-05T00:00:00.000Z')).toBe('yesterday');
    expect(fmt('2026-10-05T23:59:59.999Z')).toBe('yesterday');
  });

  it('is calendar days, not 24-hour windows: 00:01 today vs 23:59 yesterday', () => {
    const justAfterMidnight = new Date('2026-10-06T00:01:00.000Z');
    expect(fmt('2026-10-05T23:59:00.000Z', 'UTC', justAfterMidnight)).toBe('yesterday');
    expect(fmt('2026-10-06T00:00:30.000Z', 'UTC', justAfterMidnight)).toBe('today 00:00');
  });

  it('gives "D Mon" for older days of the same year and adds the year otherwise', () => {
    expect(fmt('2026-10-04T12:00:00.000Z')).toBe('4 Oct');
    expect(fmt('2026-01-05T12:00:00.000Z')).toBe('5 Jan');
    expect(fmt('2025-10-05T12:00:00.000Z')).toBe('5 Oct 2025');
    expect(fmt('2025-12-31T23:00:00.000Z')).toBe('31 Dec 2025');
  });

  it('reads the instant in the given time zone (the same moment, different local day)', () => {
    const instant = '2026-10-05T21:30:00.000Z';
    expect(fmt(instant, 'UTC')).toBe('yesterday');
    // Dubai is UTC+4: 01:30 on the 6th, i.e. today
    expect(fmt(instant, 'Asia/Dubai')).toBe('today 01:30');
    // New York is UTC-4 in October: 17:30 on the 5th, yesterday
    expect(fmt(instant, 'America/New_York')).toBe('yesterday');
  });

  it('is DST-safe across a clock change (no 24 h arithmetic)', () => {
    // London clocks went back on Sun 25 Oct 2026 (a 25-hour day); Mon 26 is "yesterday" on Tue 27.
    const now = new Date('2026-10-27T12:00:00.000Z');
    expect(fmt('2026-10-26T01:00:00.000Z', 'Europe/London', now)).toBe('yesterday');
    expect(fmt('2026-10-25T12:00:00.000Z', 'Europe/London', now)).toBe('25 Oct');
  });

  it('treats a moment later today (clock skew) as today, and a future day as a date', () => {
    expect(fmt('2026-10-06T18:00:00.000Z')).toBe('today 18:00');
    expect(fmt('2026-10-09T09:00:00.000Z')).toBe('9 Oct');
  });

  it("copes with the api's microsecond timestamps and a bare date-time", () => {
    expect(fmt('2026-10-06T10:12:03.123456Z')).toBe('today 10:12');
    expect(fmt('2026-10-06T10:12:03Z')).toBe('today 10:12');
  });

  it.each(['', 'not a date', '2026-13-45T99:99:99Z', 'undefined'])(
    'returns an empty string for an unparseable value (%j) instead of throwing',
    (bad) => {
      expect(fmt(bad)).toBe('');
    },
  );

  it('falls back to the local zone for an unknown time zone instead of throwing', () => {
    const out = formatRelativeTime('2026-10-06T10:12:03.400Z', NOW, 'Not/AZone');
    expect(out).not.toBe('');
    expect(out).toMatch(/^(today \d{2}:\d{2}|yesterday|\d{1,2} \w{3})/);
  });

  it('defaults "now" to the current clock', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-06T15:00:00.000Z'));
    expect(formatRelativeTime('2026-10-06T10:12:03.400Z', undefined, 'UTC')).toBe('today 10:12');
    expect(formatRelativeTime('2026-10-05T10:12:03.400Z', undefined, 'UTC')).toBe('yesterday');
    vi.setSystemTime(new Date('2026-10-07T00:30:00.000Z')); // past midnight: the same instant is now yesterday
    expect(formatRelativeTime('2026-10-06T10:12:03.400Z', undefined, 'UTC')).toBe('yesterday');
  });

  it('spells every month with three letters (en-GB would write "Sept")', () => {
    const expected = [
      'Jan',
      'Feb',
      'Mar',
      'Apr',
      'May',
      'Jun',
      'Jul',
      'Aug',
      'Sep',
      'Oct',
      'Nov',
      'Dec',
    ];
    expected.forEach((month, index) => {
      const iso = new Date(Date.UTC(2026, index, 1, 12)).toISOString();
      // choose a "now" a few days later so the date, not "yesterday", is shown
      expect(fmt(iso, 'UTC', new Date(Date.UTC(2026, index, 20, 12)))).toBe(`1 ${month}`);
    });
    expect(fmt('2025-09-05T12:00:00.000Z')).toBe('5 Sep 2025');
  });
});

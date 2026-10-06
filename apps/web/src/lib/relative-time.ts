/**
 * Human-readable "when" for the conversation list: `today 10:12`, `yesterday`, `5 Oct`, or
 * `5 Oct 2025` when the year differs. Pure and deterministic given `now` and `timeZone`, so it is
 * unit-tested without touching the clock or the machine's time zone.
 *
 * The api returns UTC instants (`2026-10-06T10:12:03.400Z`); people read them in their own local
 * time, so `timeZone` defaults to the browser's. Because that differs between server and
 * browser, never put this in server-rendered markup: render the rows only after mount (the chat
 * page fetches the list on the client, so the first render has none).
 */

type Ymd = { year: number; month: number; day: number };

// Month names are spelled out instead of asking Intl: `en-GB` writes September as "Sept", which
// would make one month a different width from the rest.
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

// A list renders many rows per pass; building an Intl formatter is the expensive part.
const formatters = new Map<string, Intl.DateTimeFormat>();
function formatter(
  locale: string,
  timeZone: string | undefined,
  options: Intl.DateTimeFormatOptions,
): Intl.DateTimeFormat {
  const key = `${locale}|${timeZone ?? ''}|${JSON.stringify(options)}`;
  let cached = formatters.get(key);
  if (!cached) {
    cached = new Intl.DateTimeFormat(locale, { ...options, timeZone }); // throws on a bad zone
    formatters.set(key, cached);
  }
  return cached;
}

function ymd(date: Date, timeZone: string | undefined): Ymd {
  const parts = formatter('en-CA', timeZone, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(date);
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value);
  return { year: get('year'), month: get('month'), day: get('day') };
}

/** Whole calendar days from `then` to `now`, both read in `timeZone` (DST-safe: no 24 h maths). */
function calendarDaysBetween(then: Ymd, now: Ymd): number {
  const utcDay = (d: Ymd) => Date.UTC(d.year, d.month - 1, d.day);
  return Math.round((utcDay(now) - utcDay(then)) / 86_400_000);
}

function format(iso: string, now: Date, timeZone: string | undefined): string {
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return '';

  const thenDay = ymd(then, timeZone);
  const days = calendarDaysBetween(thenDay, ymd(now, timeZone));
  if (days === 0) {
    const time = formatter('en-GB', timeZone, {
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    }).format(then);
    return `today ${time}`;
  }
  if (days === 1) return 'yesterday';

  const date = `${thenDay.day} ${MONTHS[thenDay.month - 1]}`;
  return thenDay.year === ymd(now, timeZone).year ? date : `${date} ${thenDay.year}`;
}

/**
 * `iso` as seen from `now`. An unparseable `iso` gives `''` (the list shows the title alone); a
 * bad `timeZone` falls back to the browser's. A moment later today (clock skew) is still "today".
 */
export function formatRelativeTime(iso: string, now: Date = new Date(), timeZone?: string): string {
  try {
    return format(iso, now, timeZone);
  } catch {
    // RangeError for an unknown time zone: retry in the local zone rather than show nothing.
    try {
      return format(iso, now, undefined);
    } catch {
      return '';
    }
  }
}

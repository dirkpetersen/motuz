"""
Scheduled repeating copies (managers/schedule_manager.py, README "Scheduled copies"):
the fields of a schedule and when it runs next.

A schedule runs every hour (at `minute`), every day (at `hour`:`minute`), every week
(on `weekday`, 0 = Monday ... 6 = Sunday, at `hour`:`minute`) or every month (on
`day_of_month`, 1-28 so that every month has it, at `hour`:`minute`). All of them are
UTC as stored: the browser converts the user's local time (utils/copySchedule.js).
Times are naive datetimes in UTC, as the database stores them.

Stdlib only.
"""
import datetime


HOURLY = 'hourly'
DAILY = 'daily'
WEEKLY = 'weekly'
MONTHLY = 'monthly'
FREQUENCIES = (HOURLY, DAILY, WEEKLY, MONTHLY)

# The fields each frequency uses; the others are stored as NULL
FIELDS = {
    HOURLY: ('minute',),
    DAILY: ('minute', 'hour'),
    WEEKLY: ('minute', 'hour', 'weekday'),
    MONTHLY: ('minute', 'hour', 'day_of_month'),
}
ALL_FIELDS = ('minute', 'hour', 'weekday', 'day_of_month')
RANGES = {'minute': (0, 59), 'hour': (0, 23), 'weekday': (0, 6), 'day_of_month': (1, 28)}

# Average runs per month (365.25 / 12 days), for the monthly AWS egress estimate;
# utils/copySchedule.js has the same numbers
RUNS_PER_MONTH = {HOURLY: 730.5, DAILY: 30.44, WEEKLY: 4.35, MONTHLY: 1}

WEEKDAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')


class ScheduleError(ValueError):
    """An invalid schedule; the message is for the user"""


def validate(raw):
    """
    The schedule fields of a request (a dict) as stored: {frequency, minute, hour,
    weekday, day_of_month}, the fields the frequency does not use None. ScheduleError
    if the frequency is unknown or a field it uses is missing or out of range.
    """
    if not isinstance(raw, dict):
        raise ScheduleError('A schedule needs a frequency')
    frequency = raw.get('frequency')
    if frequency not in FREQUENCIES:
        raise ScheduleError('frequency must be one of {}'.format(', '.join(FREQUENCIES)))
    spec = {'frequency': frequency}
    for name in ALL_FIELDS:
        if name not in FIELDS[frequency]:
            spec[name] = None
            continue
        value = raw.get(name)
        low, high = RANGES[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ScheduleError('{} must be a whole number from {} to {}'.format(name, low, high))
        spec[name] = value
    return spec


def spec_of(schedule):
    """The fields of a stored schedule (an object with these attributes), validated again"""
    return validate({name: getattr(schedule, name, None) for name in ('frequency',) + ALL_FIELDS})


def _naive_utc(moment):
    if moment.tzinfo is not None:
        moment = moment.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return moment


def _add_month(moment):
    if moment.month == 12:
        return moment.replace(year=moment.year + 1, month=1)
    return moment.replace(month=moment.month + 1)


def next_run(spec, after):
    """
    The first time of the schedule `spec` (validated) strictly after `after` (a naive
    UTC datetime, or an aware one), as a naive UTC datetime at a whole minute
    """
    after = _naive_utc(after)
    minute = spec['minute']
    frequency = spec['frequency']
    base = after.replace(second=0, microsecond=0)
    if frequency == HOURLY:
        candidate = base.replace(minute=minute)
        if candidate <= after:
            candidate += datetime.timedelta(hours=1)
        return candidate
    candidate = base.replace(hour=spec['hour'], minute=minute)
    if frequency == DAILY:
        if candidate <= after:
            candidate += datetime.timedelta(days=1)
        return candidate
    if frequency == WEEKLY:
        candidate += datetime.timedelta(days=(spec['weekday'] - candidate.weekday()) % 7)
        if candidate <= after:
            candidate += datetime.timedelta(days=7)
        return candidate
    # Monthly: day_of_month <= 28 exists in every month
    candidate = candidate.replace(day=spec['day_of_month'])
    if candidate <= after:
        candidate = _add_month(candidate)
    return candidate


def describe(spec):
    """'every day at 02:30 UTC' and so on, for logs and the API"""
    frequency = spec['frequency']
    if frequency == HOURLY:
        return 'every hour at minute {:02d} (UTC)'.format(spec['minute'])
    time = '{:02d}:{:02d} UTC'.format(spec['hour'], spec['minute'])
    if frequency == DAILY:
        return 'every day at {}'.format(time)
    if frequency == WEEKLY:
        return 'every {} at {}'.format(WEEKDAYS[spec['weekday']], time)
    return 'on day {} of every month at {}'.format(spec['day_of_month'], time)


def iso_utc(moment):
    """'2026-10-18T14:05:00Z' for a naive UTC datetime (None stays None)"""
    if moment is None:
        return None
    return _naive_utc(moment).replace(microsecond=0).isoformat() + 'Z'

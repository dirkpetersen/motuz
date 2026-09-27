"""
Modification times in file listings.

The API always carries them as ISO 8601 in UTC with a `Z` suffix and whole seconds
(`2026-09-27T04:57:00Z`), or None when unknown. Never a naive local time: the server's
or container's TZ must not matter.
"""
import datetime
import re


_UTC = datetime.timezone.utc

# rclone's ModTime is RFC 3339 with up to 9 fractional digits and an offset,
# e.g. 2026-09-27T04:57:00.123456789Z or 2026-09-26T21:57:00-07:00
_RFC3339 = re.compile(
    r'^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?'
    r'(?:([Zz])|([+-])(\d{2}):?(\d{2}))$'
)

# Before this, a time is a placeholder (Go's zero time 0001-01-01, the Unix epoch of
# a missing value), not a real modification time
_EARLIEST = datetime.datetime(1971, 1, 1, tzinfo=_UTC)


def iso_utc(moment):
    """An aware datetime as ISO 8601 UTC ('...Z', whole seconds), None if implausible"""
    moment = moment.astimezone(_UTC)
    if moment < _EARLIEST:
        return None
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


def epoch_to_iso_utc(value):
    """
    Epoch seconds (int, float or their string form, e.g. from `ls --time-style=+%s`)
    to ISO 8601 UTC. Epoch seconds are UTC by definition, so TZ does not matter.
    Returns None for unknown values ('?', '', None) and implausible ones.
    """
    if value is None:
        return None
    try:
        seconds = int(float(value))
        return iso_utc(datetime.datetime.fromtimestamp(seconds, tz=_UTC))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def rfc3339_to_iso_utc(value):
    """rclone's ModTime (any offset) normalized to ISO 8601 UTC, None if unknown"""
    if not isinstance(value, str):
        return None
    match = _RFC3339.match(value.strip())
    if match is None:
        return None
    year, month, day, hour, minute, second, zulu, sign, off_h, off_m = match.groups()
    try:
        if zulu:
            tz = _UTC
        else:
            offset = datetime.timedelta(hours=int(off_h), minutes=int(off_m))
            tz = datetime.timezone(offset if sign == '+' else -offset)
        moment = datetime.datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), tzinfo=tz)
        return iso_utc(moment)
    except (ValueError, OverflowError):
        return None

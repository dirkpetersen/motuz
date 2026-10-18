"""
AWS egress cost of a copy (New Copy Job dialog, copy_job_manager.create): whether a
job reads data out of Amazon S3, which AWS charge applies and a range of its price.

Motuz cannot know the account's monthly volume, discounts, Direct Connect or the AWS
Global Data Egress Waiver, so the estimate for data leaving AWS is a range: the
lowest and the highest per-GB rate of the table below times the size. README "AWS
egress cost" has the same table; update both together.

Stdlib only: it reads connection objects (models/cloud_connection.py) by attribute.
"""
import math
import re
from collections import namedtuple

from . import s3_providers

# AWS S3 data transfer OUT pricing, standard regions (source: aws.amazon.com/s3/pricing,
# "Data Transfer" tab, as of 2026-10). Tiers per month and account, in decimal GB.
PRICING_DATE = '2026-10'
GB = 1000 ** 3
FREE_GB_PER_MONTH = 100  # aggregated across all AWS services and regions of the account
INTERNET_TIERS = (       # (GB per month up to, USD per GB); None: everything above
    (10000, 0.09),       # first 10 TB
    (50000, 0.085),      # next 40 TB
    (150000, 0.07),      # next 100 TB
    (None, 0.05),        # over 150 TB
)
INTER_REGION_PER_GB = 0.02  # to another AWS region
SAME_REGION_PER_GB = 0.0    # within the same region

RATE_HIGH_PER_GB = max(rate for _, rate in INTERNET_TIERS)
RATE_LOW_PER_GB = min(rate for _, rate in INTERNET_TIERS)

# rclone's region of an AWS connection without one
DEFAULT_REGION = 'us-east-1'
REGION_RE = re.compile(r'^[a-z]{2}(-[a-z]+)+-\d$')  # as utils/ec2_config.py

INTERNET = 'internet'
INTER_REGION = 'inter-region'
SAME_REGION = 'same-region'
KINDS = (INTERNET, INTER_REGION, SAME_REGION)
CONFIRM_KINDS = frozenset((INTERNET, INTER_REGION))


def is_aws(connection):
    """
    True for an S3 connection on Amazon S3: s3_provider AWS, or (stored before
    s3_provider existed) none and no endpoint. A profile connection from an rclone
    remote takes the remote's own provider (rclone_connection._formatCredentials), so
    its stored provider says nothing: unknown, no estimate.
    """
    if connection is None or getattr(connection, 'type', None) != 's3':
        return False
    if getattr(connection, 'subtype', None) == 'profile' and getattr(connection, 'profile_source', None) != 'aws':
        return False
    provider = (getattr(connection, 's3_provider', None) or '').strip()
    if not provider:
        provider = s3_providers.legacy_provider((getattr(connection, 's3_endpoint', None) or '').strip())
    return provider == 'AWS'


def region(connection):
    """
    The AWS region of an AWS connection; rclone's default for a connection without one,
    None (unknown) for a profile without one (~/.aws/config may name it)
    """
    value = (getattr(connection, 's3_region', None) or '').strip().lower()
    if value:
        return value
    return None if getattr(connection, 'subtype', None) == 'profile' else DEFAULT_REGION


# The AWS charge of a job: kind, how many transfers between regions it makes (the read
# out of the source's region, the write into another destination region) and why, a
# phrase after "this copy" ("runs on this server, which is not in AWS, so ...")
Assessment = namedtuple('Assessment', 'kind legs reason')

RANK = {SAME_REGION: 0, INTER_REGION: 1, INTERNET: 2}


def parse_region(name, value):
    """A region setting (e.g. MOTUZ_CENTRAL_AWS_REGION): None if empty; ValueError if invalid"""
    value = (value or '').strip()
    if not value:
        return None
    if not REGION_RE.match(value):
        raise ValueError('{}={!r} is not an AWS region like us-west-2'.format(name, value))
    return value


def assess(src, dst, runner_region, runs_on):
    """
    The AWS charge of a copy from `src` to `dst` (None: the local filesystem of the
    machine that runs it), run by a machine in the AWS region `runner_region` (None: not
    in AWS or not known; `runs_on` describes it). rclone moves the data through that
    machine, so the source's region, the runner's and the destination's matter. None if
    the source is not on AWS.
    """
    if not is_aws(src):
        return None
    src_region = region(src)
    if runner_region is None:
        return Assessment(INTERNET, 0, 'runs on {}, so the data leaves AWS'.format(runs_on))
    if dst is not None and not is_aws(dst):
        return Assessment(INTERNET, 0, 'runs on {} and writes outside AWS, so the data leaves AWS'.format(runs_on))
    parts = []
    if src_region != runner_region:
        parts.append('the data is read out of {}'.format(src_region or "the source's region (not known)"))
    if dst is not None:
        dst_region = region(dst)
        if dst_region != runner_region:
            parts.append('written to {}'.format(dst_region or "the destination's region (not known)"))
    if not parts:
        return Assessment(SAME_REGION, 0, 'runs on {}, in the region of the data'.format(runs_on))
    return Assessment(INTER_REGION, len(parts), 'runs on {}, so {}'.format(runs_on, ' and '.join(parts)))


def worst(assessments):
    """The most expensive of several assessments (None for none)"""
    assessments = [a for a in assessments if a is not None]
    if not assessments:
        return None
    return max(assessments, key=lambda a: (RANK[a.kind], a.legs))


def tiered_usd(size_bytes):
    """
    The price of `size_bytes` leaving AWS to the internet if it were the account's only
    transfer of the month: the free 100 GB, then the tiers
    """
    remaining = max(0.0, size_bytes / GB - FREE_GB_PER_MONTH)
    total, lower = 0.0, 0
    for upper, rate in INTERNET_TIERS:
        span = remaining if upper is None else min(remaining, upper - lower)
        total += span * rate
        remaining -= span
        if remaining <= 0 or upper is None:
            break
        lower = upper
    return total


def _rates(job_kind, legs):
    if job_kind == INTERNET:
        return RATE_LOW_PER_GB, RATE_HIGH_PER_GB
    if job_kind == INTER_REGION:
        rate = round(INTER_REGION_PER_GB * max(1, legs), 4)
        return rate, rate
    return SAME_REGION_PER_GB, SAME_REGION_PER_GB


def _note(job_kind):
    if job_kind == INTERNET:
        return ('AWS charges ${:.2f}-${:.2f} per GB for data leaving AWS, depending on the '
                "account's monthly volume; the first {} GB per month are free; discounts or "
                'an egress waiver may lower it. Prices as of {}.'.format(
                    RATE_LOW_PER_GB, RATE_HIGH_PER_GB, FREE_GB_PER_MONTH, PRICING_DATE))
    if job_kind == INTER_REGION:
        return 'AWS charges about ${:.2f} per GB for each transfer between AWS regions. Prices as of {}.'.format(
            INTER_REGION_PER_GB, PRICING_DATE)
    return 'No AWS egress fee within the same region.'


def _number(value):
    """A finite non-negative number, else None"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def estimate(assessment, size_bytes=None, files=None, runs_on=None):
    """
    The estimate the API returns for a job with this Assessment (None: not from AWS) and
    size (None: not measured): low_usd/high_usd are None when the size is unknown
    """
    if assessment is None:
        return {'applies': False}
    job_kind = assessment.kind
    low_rate, high_rate = _rates(job_kind, assessment.legs)
    size_bytes = _number(size_bytes)
    files = _number(files)
    result = {
        'applies': True,
        'kind': job_kind,
        'bytes': int(size_bytes) if size_bytes is not None else None,
        'files': int(files) if files is not None else None,
        'low_usd': None,
        'high_usd': None,
        'rate_low_per_gb': low_rate,
        'rate_high_per_gb': high_rate,
        'free_gb_per_month': FREE_GB_PER_MONTH if job_kind == INTERNET else 0,
        'pricing_date': PRICING_DATE,
        'runs_on': runs_on,
        'reason': assessment.reason,
        'note': _note(job_kind),
    }
    if size_bytes is not None:
        result['low_usd'] = round(size_bytes / GB * low_rate, 2)
        result['high_usd'] = round(size_bytes / GB * high_rate, 2)
    return result


def needs_confirmation(assessment):
    return assessment is not None and assessment.kind in CONFIRM_KINDS


def confirmed(assessment, raw, runs_on=None):
    """
    The estimate stored on a job whose user confirmed `raw` (the dialog's estimate with
    confirmed: true): kind, rates and reason from the server, only the measured size
    from the request, the prices computed again. None if `raw` is not a confirmation.
    """
    if not isinstance(raw, dict) or raw.get('confirmed') is not True:
        return None
    result = estimate(assessment, raw.get('bytes'), raw.get('files'), runs_on)
    result.pop('applies', None)
    result.pop('note', None)
    result['confirmed'] = True
    return result


def repeated(stored, bytes_per_run, runs_per_month):
    """
    The monthly cost of a scheduled copy (managers/schedule_manager.py) whose runs each
    copy `bytes_per_run` new bytes (None: not known), at the rates of the confirmed
    estimate `stored`: later runs only copy new files, so the first run's size says
    nothing about them. {new_bytes_per_run, runs_per_month, monthly_low_usd,
    monthly_high_usd}, the prices None when the size is not known.
    """
    size = _number(bytes_per_run)
    runs = _number(runs_per_month) or 0
    result = {
        'new_bytes_per_run': int(size) if size is not None else None,
        'runs_per_month': runs,
        'monthly_low_usd': None,
        'monthly_high_usd': None,
    }
    low_rate, high_rate = _number(stored.get('rate_low_per_gb')), _number(stored.get('rate_high_per_gb'))
    if size is not None and low_rate is not None and high_rate is not None:
        result['monthly_low_usd'] = round(size / GB * runs * low_rate, 2)
        result['monthly_high_usd'] = round(size / GB * runs * high_rate, 2)
    return result

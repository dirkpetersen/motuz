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


def kind(src, dst):
    """
    Which AWS charge a copy from `src` to `dst` (None: the local filesystem) causes:
    None (the source is not on AWS), SAME_REGION, INTER_REGION or INTERNET. Two AWS
    connections whose regions are not both known count as INTER_REGION.
    """
    if not is_aws(src):
        return None
    if not is_aws(dst):
        return INTERNET
    src_region, dst_region = region(src), region(dst)
    if src_region is not None and src_region == dst_region:
        return SAME_REGION
    return INTER_REGION


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


def _rates(job_kind):
    if job_kind == INTERNET:
        return RATE_LOW_PER_GB, RATE_HIGH_PER_GB
    if job_kind == INTER_REGION:
        return INTER_REGION_PER_GB, INTER_REGION_PER_GB
    return SAME_REGION_PER_GB, SAME_REGION_PER_GB


def _note(job_kind, region_known):
    if job_kind == INTERNET:
        return ('AWS charges ${:.2f}-${:.2f} per GB for data leaving AWS, depending on the '
                "account's monthly volume; the first {} GB per month are free; discounts or "
                'an egress waiver may lower it. Prices as of {}.'.format(
                    RATE_LOW_PER_GB, RATE_HIGH_PER_GB, FREE_GB_PER_MONTH, PRICING_DATE))
    if job_kind == INTER_REGION:
        text = 'AWS charges about ${:.2f} per GB between AWS regions'.format(INTER_REGION_PER_GB)
        if not region_known:
            text += ' (the region of a connection is not known; within the same region it is free)'
        return text + '. Prices as of {}.'.format(PRICING_DATE)
    return 'No AWS egress fee within the same region.'


def _number(value):
    """A finite non-negative number, else None"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def estimate(job_kind, size_bytes=None, files=None, region_known=True):
    """
    The estimate the API returns for a job of this kind (None: not from AWS) and size
    (None: not measured): low_usd/high_usd are None when the size is unknown
    """
    if job_kind is None:
        return {'applies': False}
    low_rate, high_rate = _rates(job_kind)
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
        'region_known': bool(region_known),
        'note': _note(job_kind, region_known),
    }
    if size_bytes is not None:
        result['low_usd'] = round(size_bytes / GB * low_rate, 2)
        result['high_usd'] = round(size_bytes / GB * high_rate, 2)
    return result


def needs_confirmation(job_kind):
    return job_kind in CONFIRM_KINDS


def confirmed(job_kind, raw):
    """
    The estimate stored on a job whose user confirmed `raw` (the dialog's estimate with
    confirmed: true): kind and rates from the server, only the measured size from the
    request, the prices computed again. None if `raw` is not a confirmation.
    """
    if not isinstance(raw, dict) or raw.get('confirmed') is not True:
        return None
    result = estimate(job_kind, raw.get('bytes'), raw.get('files'))
    result.pop('applies', None)
    result.pop('note', None)
    result.pop('region_known', None)
    result['confirmed'] = True
    return result

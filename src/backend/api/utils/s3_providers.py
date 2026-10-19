"""
S3 providers of a connection of type s3 (cloud_connection.s3_provider): rclone's own
`provider` values (rclone help backend s3, v1.75), Amazon S3 first, Ceph second, then
the others alphabetically and "Other" last. The New Connection dialog shows the same
list (src/frontend/js/utils/s3Providers.js; test_s3_options compares the two).

Stdlib only: rclone_connection and the remote worker agent use it.
"""

PROVIDERS = (
    ('AWS', 'Amazon S3'),
    ('Ceph', 'Ceph (RADOS Gateway)'),
    ('Alibaba', 'Alibaba Cloud Object Storage System (OSS) formerly Aliyun'),
    ('ArvanCloud', 'Arvan Cloud Object Storage (AOS)'),
    ('BizflyCloud', 'Bizfly Cloud Simple Storage'),
    ('ChinaMobile', 'China Mobile Ecloud Elastic Object Storage (EOS)'),
    ('Cloudflare', 'Cloudflare R2 Storage'),
    ('Cubbit', 'Cubbit DS3 Object Storage'),
    ('DigitalOcean', 'DigitalOcean Spaces'),
    ('Dreamhost', 'Dreamhost DreamObjects'),
    ('Exaba', 'Exaba Object Storage'),
    ('Fastly', 'Fastly Object Storage'),
    ('FileLu', 'FileLu S5 (S3-Compatible Object Storage)'),
    ('FlashBlade', 'Pure Storage FlashBlade Object Storage'),
    ('GCS', 'Google Cloud Storage'),
    ('HCP', 'Hitachi Content Platform (HCP)'),
    ('Hetzner', 'Hetzner Object Storage'),
    ('HuaweiOBS', 'Huawei Object Storage Service'),
    ('IBMCOS', 'IBM COS S3'),
    ('IDrive', 'IDrive e2'),
    ('ImpossibleCloud', 'Impossible Cloud Object Storage'),
    ('Intercolo', 'Intercolo Object Storage'),
    ('IONOS', 'IONOS Cloud'),
    ('Leviia', 'Leviia Object Storage'),
    ('Liara', 'Liara Object Storage'),
    ('Linode', 'Linode Object Storage'),
    ('LyveCloud', 'Seagate Lyve Cloud'),
    ('Magalu', 'Magalu Object Storage'),
    ('Mega', 'MEGA S4 Object Storage'),
    ('Minio', 'Minio Object Storage'),
    ('Netease', 'Netease Object Storage (NOS)'),
    ('Outscale', 'OUTSCALE Object Storage (OOS)'),
    ('OVHcloud', 'OVHcloud Object Storage'),
    ('Petabox', 'Petabox Object Storage'),
    ('Qiniu', 'Qiniu Object Storage (Kodo)'),
    ('Rabata', 'Rabata Cloud Storage'),
    ('RackCorp', 'RackCorp Object Storage'),
    ('Rclone', 'Rclone S3 Server'),
    ('Scaleway', 'Scaleway Object Storage'),
    ('Scality', 'Scality (RING / ARTESCA)'),
    ('SeaweedFS', 'SeaweedFS S3'),
    ('Selectel', 'Selectel Object Storage'),
    ('Servercore', 'Servercore Object Storage'),
    ('SpectraLogic', 'Spectra Logic Black Pearl'),
    ('Storj', 'Storj (S3 Compatible Gateway)'),
    ('Synology', 'Synology C2 Object Storage'),
    ('TencentCOS', 'Tencent Cloud Object Storage (COS)'),
    ('US3', 'US3 Object Storage'),
    ('Wasabi', 'Wasabi Object Storage'),
    ('Zadara', 'Zadara Object Storage'),
    ('Zata', 'Zata (S3 compatible Gateway)'),
    ('ZeroServices', 'Zero Services GmbH (ZERO-Z3)'),
    ('Other', 'Any other S3 compatible provider'),
)

PROVIDER_VALUES = frozenset(value for value, _ in PROVIDERS)

# Providers that verify Content-MD5 on every request themselves, so the post-copy check
# of rclone may be skipped for uploads to them (s3_skip_checks)
CHECK_SKIPPING_PROVIDERS = frozenset(('AWS', 'Ceph'))

# s3_skip_checks: None = rclone's full checks
SKIP_POST_COPY = 'post_copy' # --ignore-checksum: no MD5 comparison after the copy
SKIP_CRC32C = 'crc32c'       # also a CRC32C instead of an MD5 with each part (AWS only)
SKIP_ALL = 'all'             # also no checksum with the parts and
                             # AWS_REQUEST_CHECKSUM_CALCULATION=when_required
SKIP_CHECKS_VALUES = frozenset((SKIP_POST_COPY, SKIP_CRC32C, SKIP_ALL))

# Providers known to verify the x-amz-checksum-crc32c of upload parts and to store a
# full-object CRC32C: Amazon S3, and Ceph from Tentacle (20.2) on. Reef and Squid ignore
# the CRC32C: a chunked upload then fails at its first part (rclone finds no CRC32C in
# the response), uploads in one piece are still verified by their Content-MD5
CRC32C_PROVIDERS = frozenset(('AWS', 'Ceph'))
# New connections to these providers start with SKIP_CRC32C (Ceph's version is unknown)
CRC32C_DEFAULT_PROVIDERS = frozenset(('AWS',))


def default_skip_checks(provider):
    """s3_skip_checks of a new connection that does not choose one"""
    return SKIP_CRC32C if provider in CRC32C_DEFAULT_PROVIDERS else None


def stores_crc32c(provider, skip_checks):
    """
    Whether objects of an S3 connection can have a full-object CRC32C that an integrity
    check reads (job_runner.side_kind): always on Amazon S3 (also objects uploaded by
    other tools), on Ceph only for a connection that uploads with CRC32C parts (else a
    CRC32C check would do one HEAD per object for nothing)
    """
    if provider in CRC32C_DEFAULT_PROVIDERS:
        return True
    return provider in CRC32C_PROVIDERS and skip_checks == SKIP_CRC32C

# s3_chunked (default true): files from this size on are uploaded in parts (multipart);
# false: in one piece up to S3's limit of 5 GiB for one upload, so that S3 keeps their
# MD5 (ETag) for Check Integrity; larger files are still uploaded in parts
CHUNKED_UPLOAD_CUTOFF = '32Mi'
UNCHUNKED_UPLOAD_CUTOFF = '5Gi'


def legacy_provider(endpoint):
    """The provider of a connection stored before s3_provider existed"""
    return 'Other' if endpoint else 'AWS'


def effective_provider(connection):
    """
    The provider of an S3 connection as rclone gets it (rclone_connection._formatCredentials):
    s3_provider, or legacy_provider for connections from before it existed
    """
    provider = getattr(connection, 's3_provider', None)
    if provider in PROVIDER_VALUES:
        return provider
    return legacy_provider(getattr(connection, 's3_endpoint', None) or '')

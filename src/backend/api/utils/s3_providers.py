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
SKIP_ALL = 'all'             # also AWS_REQUEST_CHECKSUM_CALCULATION=when_required
SKIP_CHECKS_VALUES = frozenset((SKIP_POST_COPY, SKIP_ALL))

# s3_chunked (default true): files from this size on are uploaded in parts (multipart);
# false: in one piece up to S3's limit of 5 GiB for one upload, so that S3 keeps their
# MD5 (ETag) for Check Integrity; larger files are still uploaded in parts
CHUNKED_UPLOAD_CUTOFF = '32Mi'
UNCHUNKED_UPLOAD_CUTOFF = '5Gi'


def legacy_provider(endpoint):
    """The provider of a connection stored before s3_provider existed"""
    return 'Other' if endpoint else 'AWS'

/**
 * S3 providers of a connection of type s3: rclone's `provider` values, Amazon S3 first,
 * Ceph second. The same list as src/backend/api/utils/s3_providers.py (a backend test
 * compares them). Plain JS, so node tests can import it.
 */
export const S3_PROVIDERS = [
    {value: "AWS", label: "Amazon S3"},
    {value: "Ceph", label: "Ceph (RADOS Gateway)"},
    {value: "Alibaba", label: "Alibaba Cloud Object Storage System (OSS) formerly Aliyun"},
    {value: "ArvanCloud", label: "Arvan Cloud Object Storage (AOS)"},
    {value: "BizflyCloud", label: "Bizfly Cloud Simple Storage"},
    {value: "ChinaMobile", label: "China Mobile Ecloud Elastic Object Storage (EOS)"},
    {value: "Cloudflare", label: "Cloudflare R2 Storage"},
    {value: "Cubbit", label: "Cubbit DS3 Object Storage"},
    {value: "DigitalOcean", label: "DigitalOcean Spaces"},
    {value: "Dreamhost", label: "Dreamhost DreamObjects"},
    {value: "Exaba", label: "Exaba Object Storage"},
    {value: "Fastly", label: "Fastly Object Storage"},
    {value: "FileLu", label: "FileLu S5 (S3-Compatible Object Storage)"},
    {value: "FlashBlade", label: "Pure Storage FlashBlade Object Storage"},
    {value: "GCS", label: "Google Cloud Storage"},
    {value: "HCP", label: "Hitachi Content Platform (HCP)"},
    {value: "Hetzner", label: "Hetzner Object Storage"},
    {value: "HuaweiOBS", label: "Huawei Object Storage Service"},
    {value: "IBMCOS", label: "IBM COS S3"},
    {value: "IDrive", label: "IDrive e2"},
    {value: "ImpossibleCloud", label: "Impossible Cloud Object Storage"},
    {value: "Intercolo", label: "Intercolo Object Storage"},
    {value: "IONOS", label: "IONOS Cloud"},
    {value: "Leviia", label: "Leviia Object Storage"},
    {value: "Liara", label: "Liara Object Storage"},
    {value: "Linode", label: "Linode Object Storage"},
    {value: "LyveCloud", label: "Seagate Lyve Cloud"},
    {value: "Magalu", label: "Magalu Object Storage"},
    {value: "Mega", label: "MEGA S4 Object Storage"},
    {value: "Minio", label: "Minio Object Storage"},
    {value: "Netease", label: "Netease Object Storage (NOS)"},
    {value: "Outscale", label: "OUTSCALE Object Storage (OOS)"},
    {value: "OVHcloud", label: "OVHcloud Object Storage"},
    {value: "Petabox", label: "Petabox Object Storage"},
    {value: "Qiniu", label: "Qiniu Object Storage (Kodo)"},
    {value: "Rabata", label: "Rabata Cloud Storage"},
    {value: "RackCorp", label: "RackCorp Object Storage"},
    {value: "Rclone", label: "Rclone S3 Server"},
    {value: "Scaleway", label: "Scaleway Object Storage"},
    {value: "Scality", label: "Scality (RING / ARTESCA)"},
    {value: "SeaweedFS", label: "SeaweedFS S3"},
    {value: "Selectel", label: "Selectel Object Storage"},
    {value: "Servercore", label: "Servercore Object Storage"},
    {value: "SpectraLogic", label: "Spectra Logic Black Pearl"},
    {value: "Storj", label: "Storj (S3 Compatible Gateway)"},
    {value: "Synology", label: "Synology C2 Object Storage"},
    {value: "TencentCOS", label: "Tencent Cloud Object Storage (COS)"},
    {value: "US3", label: "US3 Object Storage"},
    {value: "Wasabi", label: "Wasabi Object Storage"},
    {value: "Zadara", label: "Zadara Object Storage"},
    {value: "Zata", label: "Zata (S3 compatible Gateway)"},
    {value: "ZeroServices", label: "Zero Services GmbH (ZERO-Z3)"},
    {value: "Other", label: "Any other S3 compatible provider"},
];

export const DEFAULT_S3_PROVIDER = 'AWS';

// Providers for which the dialog offers skipping checksum checks
export const CHECK_SKIPPING_PROVIDERS = ['AWS', 'Ceph'];

// Region filled in for a new connection to storage that is not AWS
export const NON_AWS_DEFAULT_REGION = 'us-east-1';

export function isAws(provider) {
    return (provider || DEFAULT_S3_PROVIDER) === 'AWS';
}

export function canSkipChecks(provider) {
    return CHECK_SKIPPING_PROVIDERS.includes(provider || DEFAULT_S3_PROVIDER);
}

/**
 * Whether a copy to this destination connection can be checked afterwards (Check
 * Integrity): not for S3 with chunked uploads, whose objects S3 stores without an MD5.
 * `connection` is the connection object of the list (null/undefined: local filesystem).
 */
export function canCheckIntegrity(connection) {
    if (!connection || connection.type !== 's3') {
        return true;
    }
    return connection.s3_chunked === false;
}

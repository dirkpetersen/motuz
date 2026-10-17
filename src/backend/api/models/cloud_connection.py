import datetime

from ..application import db



class CloudConnection(db.Model):
    __tablename__ = "cloud_connection"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)

    name = db.Column(db.String)
    owner = db.Column(db.String)

    type = db.Column(db.String)
    subtype = db.Column(db.String, nullable=True)
    bucket = db.Column(db.String, nullable=True)

    # subtype 'profile' (S3, Azure Blob): credentials are read from the owner's home
    # directory each time the connection is used (utils/local_credentials.py).
    # profile_source is 'aws' (~/.aws), 'rclone' (~/.config/rclone/rclone.conf) or
    # 'azure-cli' (~/.azure, profile_name is the tenant id, azure_account is kept).
    profile_source = db.Column(db.String, nullable=True)
    profile_name = db.Column(db.String, nullable=True)

    # S3 fields
    s3_access_key_id = db.Column(db.String, nullable=True)
    s3_secret_access_key = db.Column(db.String, nullable=True)
    s3_session_token = db.Column(db.String, nullable=True)
    s3_region = db.Column(db.String, nullable=True)
    kms_encryption_key_arn = db.Column(db.String, nullable=True)

    # S3 compatible fields
    s3_endpoint = db.Column(db.String, nullable=True)
    s3_v2_auth = db.Column(db.String, nullable=True)
    # rclone's provider (utils/s3_providers.py); NULL: AWS without an endpoint, else Other
    s3_provider = db.Column(db.String, nullable=True)
    # Uploads in parts from 32 MiB on (fast) or in one piece up to 5 GiB, so that S3 keeps
    # each object's MD5 and Check Integrity can verify it
    s3_chunked = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    # AWS and Ceph only: None (rclone's checks), 'post_copy' or 'all' (s3_providers)
    s3_skip_checks = db.Column(db.String, nullable=True)
    # Do not verify the TLS certificate of this connection's server (e.g. on-prem S3
    # whose certificate does not match its host name); rclone's other remote still does
    skip_tls_verify = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())

    # Azure fields
    azure_account = db.Column(db.String, nullable=True)
    azure_key = db.Column(db.String, nullable=True)
    azure_sas_url = db.Column(db.String, nullable=True)

    # Swift V1 fields
    swift_user = db.Column(db.String, nullable=True)
    swift_key = db.Column(db.String, nullable=True)
    swift_auth = db.Column(db.String, nullable=True)

    # Swift V2 additional fields
    swift_tenant = db.Column(db.String, nullable=True)

    # Google Cloud Services fields
    gcp_client_id = db.Column(db.String, nullable=True)
    gcp_service_account_credentials = db.Column(db.String, nullable=True)
    gcp_project_number = db.Column(db.String, nullable=True)
    gcp_object_acl = db.Column(db.String, nullable=True)
    gcp_bucket_acl = db.Column(db.String, nullable=True)

    # SFTP
    sftp_host = db.Column(db.String, nullable=True)
    sftp_port = db.Column(db.String, nullable=True)
    sftp_user = db.Column(db.String, nullable=True)
    sftp_pass = db.Column(db.String, nullable=True)
    sftp_key_file = db.Column(db.String, nullable=True)

    # Dropbox
    dropbox_token = db.Column(db.String, nullable=True)

    # Microsoft OneDrive
    onedrive_token = db.Column(db.String, nullable=True)
    onedrive_drive_id = db.Column(db.String, nullable=True)
    onedrive_drive_type = db.Column(db.String, nullable=True)
    # Client id of the app registration that issued onedrive_token, set by "Sign in with
    # Microsoft". NULL (pasted rclone token) means rclone's app. Server controlled.
    onedrive_client_id = db.Column(db.String, nullable=True)

    # Google Drive (rclone backend "drive", scope "drive")
    gdrive_token = db.Column(db.String, nullable=True)
    gdrive_root_folder_id = db.Column(db.String, nullable=True)
    gdrive_team_drive = db.Column(db.String, nullable=True) # Shared drive id
    # Client id of the OAuth client that issued gdrive_token, set by "Sign in with
    # Google". NULL (pasted rclone token) means rclone's app. Server controlled.
    gdrive_client_id = db.Column(db.String, nullable=True)

    # WebDAV
    webdav_url = db.Column(db.String, nullable=True)
    webdav_user = db.Column(db.String, nullable=True)
    webdav_pass = db.Column(db.String, nullable=True)

    # OAuth token broker (see managers/token_broker_manager.py): rclone receives this
    # opaque handle instead of the real refresh token
    token_broker_handle = db.Column(db.String, nullable=True, unique=True)

    created_at = db.Column(db.DateTime, default=datetime.datetime.now)


    def __repr__(self):
        return "<Cloud Connection {} ({})>".format(self.name, self.type)

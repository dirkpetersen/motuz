---
paths:
  - "src/frontend/js/views/Dialogs/CloudConnection/**"
---

# New Cloud Connection dialog

Detail for the dialog layout. The UI e2e suites check it.

Keep it unambiguous: top to bottom Type (for S3 followed by "Provider", `s3_provider` from `utils/s3Providers.js`, and for non-AWS providers the Endpoint URL), the only "Connection Name" field, the type's own part (sign-in, "Credentials" found in the home directory, or credentials), then advanced options collapsed; exactly one primary button, "Create connection", in the footer. For OneDrive/Google Drive (`CloudConnectionDialogFields` reports the sign-in provider through `onModeChange`) the footer has only Cancel and "Create connection", disabled with "Sign in first" until `OauthSignIn` reports a chosen drive (`onOauthChange`); the dialog then calls `/api/oauth/<provider>/connect/` with the Connection Name, which the sign-in prefills from the drive until the user types one. The sign-in part has no name field and no create button. Other types and "Advanced: paste a token" get "Test connection" + "Create connection". All errors (sign-in, connect, create/save via `state.api.cloudErrorMessage`, failed tests) appear once, in `DialogError` above the footer buttons. A callback (`/clouds?oauth_state=...` or `?oauth_error=...`, plus `&oauth_provider=` except for OneDrive) opens the dialog on the provider's type; the dialog reads it once in its constructor. The UI suites check this layout.

**S3 options** (`utils/s3Providers.js`, the same list as backend `utils/s3_providers.py`, compared by `test_s3_options.py`): non-AWS providers prefill the region with `us-east-1` and accept any access key format (the AWS patterns only for AWS); advanced options hold the AWS-only endpoint, "Ignore certificate errors" (`skip_tls_verify`), "Chunked uploads" (`s3_chunked`, default on) and, for AWS and Ceph only (`canSkipChecks`), "Checksum checks" (`s3_skip_checks`: empty, `post_copy`, `all` with a warning). `EditCopyJobDialog` greys out Check Integrity when `canCheckIntegrity(dst_cloud)` is false (S3 with chunked uploads).


---
paths:
  - "src/frontend/js/views/Dialogs/CloudConnection/**"
---

# New Cloud Connection dialog

Detail for the dialog layout. The UI e2e suites check it.

Keep it unambiguous: top to bottom Type, the only "Connection Name" field, the type's own part (sign-in, "Credentials" found in the home directory, or credentials), then advanced options collapsed; exactly one primary button, "Create connection", in the footer. For OneDrive/Google Drive (`CloudConnectionDialogFields` reports the sign-in provider through `onModeChange`) the footer has only Cancel and "Create connection", disabled with "Sign in first" until `OauthSignIn` reports a chosen drive (`onOauthChange`); the dialog then calls `/api/oauth/<provider>/connect/` with the Connection Name, which the sign-in prefills from the drive until the user types one. The sign-in part has no name field and no create button. Other types and "Advanced: paste a token" get "Test connection" + "Create connection". All errors (sign-in, connect, create/save via `state.api.cloudErrorMessage`, failed tests) appear once, in `DialogError` above the footer buttons. A callback (`/clouds?oauth_state=...` or `?oauth_error=...`, plus `&oauth_provider=` except for OneDrive) opens the dialog on the provider's type; the dialog reads it once in its constructor. The UI suites check this layout.

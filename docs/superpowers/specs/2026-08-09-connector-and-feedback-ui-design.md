# Connector and feedback UI adjustments

## Goals

- Show the unverified-email reminder without changing the application layout.
- Preserve the Sources page while OAuth authorization opens separately.
- Show the issue-report form only after an explicit user action.

## Design

### Verification reminder

`VerifyBanner` remains dismissible and keeps its resend action. Its styling becomes a fixed, compact overlay anchored to the lower-left viewport corner, above the application content. It uses a bounded width and responsive inset so it cannot push or resize the page.

### Connector authorization

The Sources connector picker remains a modal. Selecting a configured provider first creates the OAuth state through the existing install-url API, then opens its authorization URL in a new browser tab with `noopener`. The current Sources page remains intact. The OAuth callback continues returning the new tab to the app.

### Issue reporting

`Chat` keeps one `feedbackDraft` value. It is set only by the explicit answer-level or citation-level flag controls; the form renders only when that draft is present. Submitting or cancelling clears the draft. Answer streaming and metadata arrival do not set it.

## Failure handling

- A blocked popup shows an existing toast telling the user to allow popups; the current page remains usable.
- Existing API errors remain visible through the current toast/error handling.
- The verification reminder remains dismissible for the current browser session.

## Verification

- Add focused component/interaction coverage where the frontend test setup supports it.
- Run frontend lint and production build.
- Manually verify the overlay does not alter page dimensions, connector authorization opens a new tab, and issue reporting is closed until clicked.

# Pinterest: getting from Trial to Standard access

The Pinterest destination (`publish_pinterest` in `release_publish.py`) is implemented and tested,
but **cannot create real pins until the Pinterest app is upgraded from Trial to Standard access**.
This page records where that stands, what Pinterest requires, and what to do once it is approved.
Setup steps for the destination itself are in [release-comms.md](release-comms.md#pinterest-setup).

## Status

| Item | State |
|---|---|
| Publisher code and tests | Done. Uses the raw REST API v5 over `urllib`; no SDK. |
| Business account | `CodexCryptica` (BUSINESS). |
| OAuth token with `pins:write`, `boards:write` | Done. Stored in the daemon env file (below). |
| Target board | *Codex Cryptica Content*. |
| Creating a pin in production | **Blocked by Trial access** (see below). |
| `pinterest` in `release_comms.destinations` | **Deliberately not enabled.** |

### The blocker

Creating a pin against `https://api.pinterest.com/v5/pins` returns:

```
403  code 29  Apps with Trial access may not create Pins in production
                https://api.pinterest.com - use API Sandbox https://api-sandbox.pinterest.com instead.
```

Reads (`/user_account`, `/boards`) work. Pins created in the sandbox are visible only to the
account owner, so they cannot be used to publish anything.

**Do not add `pinterest` to `destinations` while the app is on Trial access.** Every release
would fail its Pinterest step with this 403; a failed step marks the run failed and it is retried.

## Credentials

Kept in the daemon's private env file (`~/.config/codex-pr-review/webhook.env`, mode `600`), never
in the repository:

| Variable | Purpose |
|---|---|
| `PINTEREST_ACCESS_TOKEN` | Bearer token used by the publisher. Lasts 30 days. **Not refreshed automatically.** |
| `PINTEREST_REFRESH_TOKEN` | Returned by the OAuth exchange, valid 60 days. Nothing uses it yet. |
| `PINTEREST_APP_ID`, `PINTEREST_APP_SECRET` | Needed only to mint or refresh tokens. |
| `PINTEREST_BOARD_ID` | Board that pins are created on. |

Scopes must include `boards:read`, `boards:write`, `pins:read`, `pins:write` and
`user_accounts:read`. Missing `pins:write`/`boards:write` fails with a 401 that names the missing
scopes. The app admin page has no scope picker for OAuth apps; scopes are requested in the
authorization URL.

### Minting a token with OAuth

1. Register a redirect URI on the app (it must match exactly, including case and any trailing
   slash). `https://codexcryptica.com/oauth/pinterest` works; the page may 404, since only the
   address bar matters.
2. Open the authorization URL while logged in as the business account and approve:

   ```
   https://www.pinterest.com/oauth/?client_id=<APP_ID>
     &redirect_uri=<URL-encoded redirect URI>
     &response_type=code
     &scope=boards:read,boards:write,pins:read,pins:write,user_accounts:read
     &state=<random string; check it comes back unchanged>
   ```

3. Copy `code` from the redirected URL and exchange it quickly (codes are short-lived):

   ```bash
   curl -s -X POST https://api.pinterest.com/v5/oauth/token \
     -u "$PINTEREST_APP_ID:$PINTEREST_APP_SECRET" \
     -d grant_type=authorization_code -d code="$CODE" -d redirect_uri="$REDIRECT_URI"
   ```

   The response contains `access_token`, `refresh_token` and their lifetimes. Write them to the env
   file without echoing them. For a sandbox token, use `https://api-sandbox.pinterest.com/v5/oauth/token`.

## Upgrading to Standard access

Pinterest requires ([access tiers](https://developers.pinterest.com/docs/key-concepts/access-tiers/)):

- The app is already approved for Trial and follows the Developer Guidelines.
- A **video recording of the app completing an action with the API**, including the **user
  authentication flow**. It must use an OAuth access token, not collected logins or session
  cookies, and must show how Pinterest is integrated. Wireframes or general platform demos may be
  denied.
- For an app whose only user is its owner, **a terminal or Postman screen recording is accepted**.
- Submit under **My apps → Upgrade** on the app card: confirm the use case and privacy policy link,
  upload the video, submit. Pinterest reviews by hand and suggests allowing a few days; the outcome
  is not guaranteed. The docs give no video length or format limits.

### Demo video plan (about 2 to 3 minutes)

Pin creation has to happen in the [sandbox](https://developers.pinterest.com/docs/developer-tools/sandbox/)
because the app is on Trial. Use a **sandbox token** (portal: My apps → Manage → Configure →
Generate Access Token → Sandbox, 30 days), or run the OAuth flow above against the sandbox token
endpoint so the whole video is one continuous flow.

1. **Purpose.** Caption or say: internal tool that posts our own release announcements to our own
   board; the only user is the account owner.
2. **OAuth consent.** Open the authorization URL, show Pinterest's consent screen with the scopes,
   approve, and show the redirect back with `?code=…` in the address bar.
3. **Token exchange in the terminal.** Show the granted scopes and expiry. **Never show the token.**
4. **Create a pin** with a terminal command: board, image URL, title, link, and the response with
   the pin ID. State that Trial access limits this to the sandbox.
5. **Show the result** on the account's own pinterest.com profile (sandbox pins are visible to the
   owner).
6. **Credentials.** One line: the token is stored in a private file; no passwords or cookies are used.

## After approval

1. Re-run the OAuth flow if needed (access level can change what a token may do) and check
   `POST /pins` no longer returns code 29.
2. Create one pin by hand and confirm it appears **publicly** on the board.
3. Check every page URL that will be pinned returns 200. Pins link to the draft's `pageUrl`, and
   drafts have contained invented slugs that 404 (see the repeat-announcement notes in
   [release-comms.md](release-comms.md)).
4. Add `pinterest` to `release_comms.destinations`, dry-run, then go live.
5. Consider adding token refresh (`PINTEREST_REFRESH_TOKEN` plus the app ID and secret via
   `grant_type=refresh_token`). Without it, pins silently stop when the 30-day token expires.
6. Consider surfacing Pinterest's `code` and `message` in `PublicationError`; `_http_json` currently
   reports only the status code, which hid the two failures above.

## Alternatives while waiting

- Upload pins by hand, or via Pinterest's bulk-upload CSV in the business tools (availability not
  verified for this account).
- Pinterest's RSS auto-publish for a claimed website (not investigated).

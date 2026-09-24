# Reddit Publisher Operations

This runbook configures release-comms to package approved Reddit copy into the
Devvit publisher and install it on a subreddit. Devvit receives the candidate
data inside the app build; the Reddit runtime does not fetch GitHub or any
other external domain.

## One-time setup

### 1. Choose the app checkout and subreddit

The machine running the dev-agents daemon needs a checkout of this repository
containing `apps/reddit-devvit`. Configure an absolute path in the target
project's `config/projects.yaml`:

```yaml
projects:
  codex-cryptica:
    release_comms:
      auto_publish: false
      subreddit: codexcryptica
      devvit_app_dir: /home/espen/Projects/dev-agents/apps/reddit-devvit
      devvit_subreddit: dev_agent_publish_dev
```

`devvit_subreddit` is optional and defaults to `release_comms.subreddit`. Use
the test subreddit while validating the workflow; set it to the production
subreddit only when the publisher should be installed there. The target
account must be a moderator of that subreddit.

### 2. Authenticate the daemon account

From `apps/reddit-devvit`, log in with the same operating-system account that
runs the daemon:

```bash
bunx devvit login
```

The Devvit CLI stores its login for that local account. The daemon needs the
CLI (`devvit`, `bunx`, or `npx`) on its `PATH`, an active login, and permission
to install `dev-agent-publisher` on the configured subreddit. Do not put the
Devvit token in `projects.yaml` or commit it to the repository.

You can inspect installations with:

```bash
bunx devvit list installs dev_agent_publish_dev
```

See the [Devvit CLI guide](https://developers.reddit.com/docs/guides/tools/devvit_cli)
for login, upload, and install command details.

## Release flow

With `devvit_app_dir` configured, the approved Reddit publication does this:

1. Release-comms generates the GitHub Discussion draft and its Reddit variant.
2. On an approved live release-comms run, dev-agents stages the candidate in
   the target repository's release manifest.
3. The workflow temporarily writes the approved manifest into
   `apps/reddit-devvit/src/candidates.json`, runs `devvit upload`, and installs
   `dev-agent-publisher@latest` on `devvit_subreddit`.
4. The temporary candidate file is restored locally after upload/install. The
   candidate data remains in the installed app version.
5. A subreddit moderator chooses **Release: Post Next Approved Candidate**.
   That single action publishes the newest unposted bundled candidate. No
   separate sync or enqueue action is required.

`auto_publish: false` keeps release-comms dry-run/approval behavior: run the
release-comms command with `--publish` when the draft is approved. That approval
stages and installs the candidate; it does not submit the Reddit post. The
moderator's menu action is the final publish step. The app's hourly job only
refreshes queue state; it does not publish posts in the background.

When the candidate has a page URL, Reddit receives a link post and the drafted
copy as its first comment. The app tracks posted candidate IDs in Redis to
avoid duplicate submissions.

## Troubleshooting

### Upload succeeds but install fails

The latest app version may already be uploaded. From the app directory, retry
the install as the authenticated moderator account:

```bash
bunx devvit install dev_agent_publish_dev dev-agent-publisher@latest
```

If the release-comms run reports a Devvit upload failure, retry that release
comms run. It will package the candidate manifest again and retry the upload.

### No candidate appears in the menu action

- Confirm `devvit_app_dir` points to the app checkout on the daemon host.
- Confirm the release-comms run was approved and included the `reddit`
  destination.
- Confirm the run completed the Devvit install step on the same subreddit
  where you opened the moderator menu.
- Check the release-comms tracking issue and daemon logs for the upload/install
  error.

### Domain permission error

The bundled app does not use HTTP fetch and requests no external-domain
permissions. If Reddit reports an HTTP domain denial, the subreddit is still
running an older app version; upload/install the updated build and refresh the
subreddit.

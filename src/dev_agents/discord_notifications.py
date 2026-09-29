"""Forward high-signal Mythrasgen agent events from GitHub to Discord."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from dev_agents.config import ConfigError, ProjectConfig, load_projects_config, select_project

MAX_BODY_BYTES = 1_000_000
MAX_DISCORD_CONTENT = 2000


def _signature_valid(body: bytes, received: str | None, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return received is not None and hmac.compare_digest(expected, received)


def _issue_message(project_name: str, issue: dict[str, Any], heading: str) -> str:
    number = issue.get("number", "?")
    title = str(issue.get("title") or "Untitled issue")
    url = str(issue.get("html_url") or "")
    return f"**{project_name} · {heading}**\n**{title}** (#{number})\n{url}"[:MAX_DISCORD_CONTENT]


def _notification(project_name: str, github: str, event: str, payload: dict[str, Any]) -> str | None:
    repository = payload.get("repository")
    if not isinstance(repository, dict) or repository.get("full_name") != github:
        return None

    action = payload.get("action")
    if event == "workflow_run" and action == "completed":
        workflow = payload.get("workflow")
        run = payload.get("workflow_run")
        if (
            not isinstance(workflow, dict)
            or not isinstance(run, dict)
            or workflow.get("path") != ".github/workflows/deploy.yml"
            or run.get("head_branch") != "main"
        ):
            return None
        conclusion = str(run.get("conclusion") or "unknown")
        if conclusion not in {"failure", "timed_out", "startup_failure"}:
            return None
        heading = "deploy failed" if conclusion == "failure" else f"deploy {conclusion.replace('_', ' ')}"
        run_number = run.get("run_number", "?")
        url = str(run.get("html_url") or "")
        return f"**{project_name} · {heading}**\nGitHub Pages · main · run #{run_number}\n{url}"[:MAX_DISCORD_CONTENT]

    if event == "issues":
        issue = payload.get("issue")
        if not isinstance(issue, dict):
            return None
        labels = {
            str(label.get("name", "")).lower()
            for label in issue.get("labels", [])
            if isinstance(label, dict)
        }
        label = payload.get("label")
        agent_added = isinstance(label, dict) and str(label.get("name", "")).lower() == "agent"
        if (action == "labeled" and agent_added) or (
            action in {"opened", "reopened"} and "agent" in labels
        ):
            return _issue_message(project_name, issue, "agent issue queued")
        return None

    if event == "pull_request":
        pull = payload.get("pull_request")
        if not isinstance(pull, dict) or "<!-- dev-agents:issue-fix " not in str(pull.get("body") or ""):
            return None
        if action == "opened":
            number = pull.get("number", "?")
            title = str(pull.get("title") or "Untitled PR")
            url = str(pull.get("html_url") or "")
            return f"**{project_name} · agent PR opened**\n**{title}** (#{number})\n{url}"[:MAX_DISCORD_CONTENT]
    return None


def _discord_url(project: ProjectConfig) -> str:
    notification_config = project.discord_notifications
    if notification_config is None:
        raise ConfigError("Discord notifications are not configured for this project")
    url = os.environ.get(notification_config.webhook_env, "").strip()
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"discord.com", "discordapp.com"}
        or not parsed.path.startswith("/api/webhooks/")
    ):
        raise ConfigError(f"Set a valid Discord webhook in {notification_config.webhook_env}")
    return url


def _send_discord(url: str, content: str) -> None:
    body = json.dumps(
        {"content": content[:MAX_DISCORD_CONTENT], "allowed_mentions": {"parse": []}}
    ).encode()
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "dev-agents"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=8) as response:
            if response.status not in {200, 204}:
                raise RuntimeError(f"Discord returned HTTP {response.status}")
    except (HTTPError, URLError, TimeoutError) as error:
        raise RuntimeError(f"Discord notification failed: {error}") from error


def serve_discord_notifications(project_name: str, project: ProjectConfig) -> None:
    notification_config = project.discord_notifications
    if notification_config is None:
        raise ConfigError(f"Project {project_name} has no discord_notifications configuration")
    if project.pr_fixer is None:
        raise ConfigError(f"Project {project_name} needs pr_fixer config for its GitHub webhook secret")
    secret = os.environ.get(project.pr_fixer.webhook_secret_env)
    if not secret:
        raise ConfigError(f"Set {project.pr_fixer.webhook_secret_env} before starting notifications")
    discord_url = _discord_url(project)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            healthy = self.path == "/health"
            self.send_response(200 if healthy else 404)
            self.end_headers()
            if healthy:
                self.wfile.write(b'{"ok":true}')

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            if length > MAX_BODY_BYTES:
                self.send_response(413)
                self.end_headers()
                return
            body = self.rfile.read(length)
            delivery = self.headers.get("X-GitHub-Delivery", "unknown")
            if self.path != notification_config.webhook_path or not _signature_valid(
                body, self.headers.get("X-Hub-Signature-256"), secret
            ):
                print(f"[discord-notifications] rejected delivery={delivery} reason=invalid-path-or-signature", flush=True)
                self.send_response(401)
                self.end_headers()
                return
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return
            event = self.headers.get("X-GitHub-Event", "")
            message = _notification(project_name, str(project.github or ""), event, payload)
            if message is None:
                self.send_response(200)
                self.end_headers()
                return
            try:
                _send_discord(discord_url, message)
            except Exception as error:  # noqa: BLE001 - notification failure must not affect the agent daemon
                print(f"[discord-notifications] failed delivery={delivery} event={event} error={error}", flush=True)
                self.send_response(502)
                self.end_headers()
                return
            print(f"[discord-notifications] sent delivery={delivery} event={event}", flush=True)
            self.send_response(202)
            self.end_headers()

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"[discord-notifications] {fmt % args}", flush=True)

    server = ThreadingHTTPServer(("127.0.0.1", notification_config.port), Handler)
    server.daemon_threads = True
    print(
        f"[discord-notifications] listening on 127.0.0.1:{notification_config.port}"
        f"{notification_config.webhook_path} for {project.github}",
        flush=True,
    )
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Forward project agent events to Discord")
    parser.add_argument("project")
    parser.add_argument("--config", type=Path, default=Path("config/projects.yaml"))
    args = parser.parse_args()
    config = load_projects_config(args.config)
    serve_discord_notifications(args.project, select_project(config, args.project))


if __name__ == "__main__":
    main()

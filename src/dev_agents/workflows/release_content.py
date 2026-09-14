"""Local release content generation: evaluator/writer passes and derivation.

The generic orchestration lives here; product-specific wording comes from
prompt templates in the target checkout (``docs/release-comms/*.md``) with
built-in fallbacks, so target repositories keep their own voice.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dev_agents.runtime import run_agent, run_with_fallback


@dataclass(frozen=True)
class ReleaseFeature:
    name: str
    why_users_care: str
    bluesky_worthy: bool = False


@dataclass(frozen=True)
class EvaluatorResult:
    postworthy: bool
    reason: str
    importance: str = "medium"
    features: list[dict[str, Any]] = field(default_factory=list)
    recommended_channels: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WriterResult:
    # Kept for state-file compatibility. Discord is derived from Bluesky copy
    # at publication time and this field is intentionally ignored.
    bluesky: list[dict[str, str]] = field(default_factory=list)
    github_discussions: list[dict[str, str]] = field(default_factory=list)
    discord: str | None = None


@dataclass(frozen=True)
class ReleaseDelta:
    """Read-only facts about what changed between two SHAs."""

    new_sha: str
    previous_sha: str | None
    commits: list[str]
    files_changed: list[str]
    diff_stat: str


BLUESKY_CHARACTER_LIMIT = 300
SKILLS_DIR = Path(__file__).resolve().parents[3] / "skills"


def load_skill_prompt(skill_id: str) -> str:
    """Load a skill body for prompt rendering, minus its frontmatter."""
    if not re.fullmatch(r"[a-z0-9_-]+", skill_id):
        raise ValueError(f"invalid skill id: {skill_id!r}")
    text = (SKILLS_DIR / skill_id / "SKILL.md").read_text(encoding="utf-8")
    if text.startswith("---"):
        return text.split("---", 2)[2].lstrip("\n")
    return text


def extract_json_block(text: str) -> dict[str, Any] | None:
    """Extract first json object from text (fenced or unfenced)."""
    fenced = re.search(r"```json\s*([\s\S]*?)```", text, re.IGNORECASE)
    candidate = fenced.group(1) if fenced else text
    match = re.search(r"\{[\s\S]*\}", candidate)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def render_template(template: str, context: dict[str, str]) -> str:
    """Substitute ``{key}`` placeholders, leaving unknown braces intact."""
    rendered = template
    for key, value in context.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


def _git_text(repo: Path, args: list[str], timeout: float) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"git {' '.join(args)} failed: {error}") from error
    if proc.returncode:
        message = proc.stderr.strip() or proc.stdout.strip() or "git command failed"
        raise RuntimeError(f"git {' '.join(args)} failed: {message}")
    return proc.stdout.strip()


def _sha_present(repo: Path, sha: str) -> bool:
    try:
        proc = subprocess.run(
            ["git", "cat-file", "-e", sha],
            cwd=repo,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def ensure_shas_present(
    repo: Path,
    new_sha: str,
    previous_sha: str | None = None,
    timeout_seconds: float = 120,
) -> None:
    """Fetch from origin so delta SHAs resolve locally.

    The daemon works in a long-lived checkout that may predate the release.
    ``git fetch`` only adds objects and moves remote-tracking refs; it never
    touches the working tree. Fetch failures are ignored so the subsequent
    git commands still raise the decisive error.
    """
    wanted = [new_sha] if not previous_sha else [new_sha, previous_sha]
    if all(_sha_present(repo, sha) for sha in wanted):
        return
    try:
        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=repo,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    for sha in wanted:
        if _sha_present(repo, sha):
            continue
        try:
            subprocess.run(
                ["git", "fetch", "origin", sha],
                cwd=repo,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue


def collect_release_delta(
    repo: Path, new_sha: str, previous_sha: str | None, timeout_seconds: float = 60
) -> ReleaseDelta:
    """Collect commits, files, and diff stat between two SHAs (read-only)."""
    ensure_shas_present(repo, new_sha, previous_sha)
    if previous_sha:
        log_range = [f"{previous_sha}..{new_sha}"]
    else:
        log_range = ["-10", new_sha]
    commits = _git_text(repo, ["log", "--format=%h %s", *log_range, "--"], timeout_seconds)
    files = _git_text(
        repo,
        ["diff", "--name-only", f"{previous_sha}..{new_sha}", "--"]
        if previous_sha
        else ["show", "--format=", "--name-only", new_sha, "--"],
        timeout_seconds,
    )
    stat = _git_text(
        repo,
        ["diff", "--stat", f"{previous_sha}..{new_sha}", "--"]
        if previous_sha
        else ["show", "--format=", "--stat", new_sha, "--"],
        timeout_seconds,
    )
    return ReleaseDelta(
        new_sha=new_sha,
        previous_sha=previous_sha,
        commits=commits.splitlines()[:50],
        files_changed=[line for line in files.splitlines() if line][:100],
        diff_stat=stat[:4000],
    )


def parse_evaluator_result(data: dict[str, Any] | None) -> EvaluatorResult | None:
    """Validate an evaluator JSON payload; None when unusable."""
    if not isinstance(data, dict) or not isinstance(data.get("postworthy"), bool):
        return None
    features = data.get("features")
    channels = data.get("recommended_channels")
    return EvaluatorResult(
        postworthy=data["postworthy"],
        reason=str(data.get("reason", "")),
        importance=str(data.get("importance", "medium")),
        features=[item for item in features if isinstance(item, dict)]
        if isinstance(features, list)
        else [],
        recommended_channels=[item for item in channels if isinstance(item, str)]
        if isinstance(channels, list)
        else [],
    )


def parse_writer_result(data: dict[str, Any] | None) -> WriterResult | None:
    """Validate a writer JSON payload; None when unusable."""
    if not isinstance(data, dict):
        return None
    bluesky = data.get("bluesky")
    discussions = data.get("github_discussions")
    discussion_drafts: list[dict[str, str]] = []
    if isinstance(discussions, list):
        for item in discussions:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", ""))
            body = str(item.get("body", ""))
            if not (title.strip() or body.strip()):
                continue
            discussion = {"title": title, "body": body}
            page_url = str(item.get("pageUrl", "")).strip()
            image = str(item.get("image", "")).strip()
            if page_url:
                discussion["pageUrl"] = page_url
            if image:
                discussion["image"] = image
            discussion_drafts.append(discussion)
    return WriterResult(
        bluesky=[
            {
                "pageUrl": str(item.get("pageUrl", "")),
                "text": str(item.get("text", "")),
                "image": str(item.get("image", "")).strip(),
            }
            for item in bluesky
            if isinstance(item, dict) and str(item.get("text", "")).strip()
        ]
        if isinstance(bluesky, list)
        else [],
        github_discussions=discussion_drafts,
    )


def prepare_bluesky_text(text: str, page_url: str) -> str:
    """Fit Bluesky copy plus URL within the character limit."""
    body = " ".join(text.strip().split())
    url = page_url.strip()
    suffix = f" {url}" if url and url not in body else ""
    budget = BLUESKY_CHARACTER_LIMIT - len(suffix)
    if budget <= 0:
        return suffix.strip()
    if len(body) > budget:
        body = (body[: budget - 1].rstrip() + "…") if budget > 1 else ""
    return (body + suffix).strip()


def derive_discord_message(text: str) -> str:
    """Derive one Discord message from one Bluesky message without hashtags."""
    return re.sub(r"\s+", " ", re.sub(r"#[\w-]+", "", " ".join(text.split()))).strip()


def derive_discord_from_bluesky(texts: list[str]) -> str:
    """Derive legacy combined Discord copy from Bluesky drafts."""
    return "\n\n".join(
        message for message in (derive_discord_message(text) for text in texts) if message
    )


ASSET_CDN_BASE_URL = "https://assets.codexcryptica.com"
ASSET_DB_RELATIVE_PATH = Path("docs/deployment/r2-asset-db.md")
ASSET_DB_SECTION = "## `announcements/`"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

IMAGE_PROMPT_TEMPLATE = """\
Create one announcement illustration for this release post and save it as a PNG
file to exactly this path: {dest}

Post copy (subject matter, not text to render):
{subject}

Constraints:
- PNG format, landscape orientation, around 1600x1000 pixels.
- Illustration or scene only: do NOT render any words, letters, numbers, logos,
  or watermarks in the image.
- Reply with only DONE when the file is saved, or FAILED when it cannot be done.
No other prose."""


@dataclass(frozen=True)
class ImageStatus:
    """Resolution of one draft's suggested art."""

    page_url: str
    image: str
    status: str
    size: str = ""
    mime: str = ""
    detail: str = ""


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.0f} MB"


def _head_asset(url: str, timeout_seconds: float) -> ImageStatus | None:
    """HEAD an asset URL; None when it does not resolve."""
    request = urllib.request.Request(
        url, headers={"User-Agent": "dev-agents/release-comms"}, method="HEAD"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            length = response.headers.get("Content-Length", "")
            return ImageStatus(
                page_url="",
                image=url,
                status="ok",
                size=_human_size(int(length)) if length.isdigit() else "?",
                mime=response.headers.get("Content-Type", "").split(";")[0].strip(),
                detail=f"http {response.status}",
            )
    except (urllib.error.HTTPError, OSError, ValueError):
        return None


def load_asset_inventory(
    repo: Path, *, db_relative_path: Path = ASSET_DB_RELATIVE_PATH
) -> list[str]:
    """List image keys recorded in the asset DB snapshot (best effort)."""
    try:
        text = (repo / db_relative_path).read_text(encoding="utf-8")
    except OSError:
        return []
    keys = re.findall(
        r"`((?:announcements|screenshots|og|images)/[^`]+?\.(?:png|jpg|jpeg|webp))`",
        text,
    )
    return sorted(set(keys))


def verify_draft_images(
    drafts: list[dict[str, str]],
    *,
    base_url: str = ASSET_CDN_BASE_URL,
    timeout_seconds: float = 30,
) -> list[ImageStatus]:
    """Check each draft's suggested art resolves; never raises."""
    statuses: list[ImageStatus] = []
    for draft in drafts:
        page_url = draft.get("pageUrl", "")
        image = draft.get("image", "").strip()
        if not image:
            statuses.append(ImageStatus(page_url, "", "unspecified", detail="writer named no art"))
        elif image.startswith("capture:"):
            statuses.append(
                ImageStatus(page_url, image, "capture-requested", detail="needs screenshot")
            )
        else:
            resolved = _head_asset(
                image
                if image.startswith(("http://", "https://"))
                else f"{base_url.rstrip('/')}/{image.split('?', 1)[0].lstrip('/')}",
                timeout_seconds,
            )
            if resolved is None:
                statuses.append(
                    ImageStatus(page_url, image, "missing", detail="url does not resolve")
                )
            else:
                statuses.append(
                    ImageStatus(
                        page_url, image, "ok", resolved.size, resolved.mime, resolved.detail
                    )
                )
    return statuses


def sync_asset_db(
    repo: Path,
    statuses: list[ImageStatus],
    purpose: str,
    *,
    db_relative_path: Path = ASSET_DB_RELATIVE_PATH,
    base_url: str = ASSET_CDN_BASE_URL,
    today: str | None = None,
) -> list[str]:
    """Append DB rows for resolving assets the snapshot lacks; returns added keys."""
    path = repo / db_relative_path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    day = today or datetime.now(UTC).date().isoformat()
    rows: list[str] = []
    for status in statuses:
        if status.status != "ok" or not status.image or status.image in text:
            continue
        asset_url = (
            status.image
            if status.image.startswith(("http://", "https://"))
            else f"{base_url.rstrip('/')}/{status.image}"
        )
        rows.append(
            f"| [`{status.image}`]({asset_url}) "
            f"| {status.size or '?'} | {status.mime or '?'} | {day} | {purpose} |"
        )
    if not rows:
        return []
    section = text.find(ASSET_DB_SECTION)
    if section < 0:
        text = (
            text.rstrip("\n")
            + "\n\n"
            + ASSET_DB_SECTION
            + " (pipeline-added)\n\n"
            + "\n".join(rows)
            + "\n"
        )
    else:
        following = text.find("\n## ", section + len(ASSET_DB_SECTION))
        block = "\n".join(rows) + "\n"
        if following < 0:
            text = text.rstrip("\n") + "\n" + block
        else:
            text = text[:following].rstrip("\n") + "\n" + block + text[following:]
    path.write_text(text, encoding="utf-8")
    return [row.split("`")[1] for row in rows]


def is_png_image(path: Path) -> bool:
    """True when a file exists and starts with the PNG magic bytes."""
    try:
        with path.open("rb") as stream:
            return stream.read(len(PNG_MAGIC)) == PNG_MAGIC and path.stat().st_size > 0
    except OSError:
        return False


def generate_announcement_image(
    *,
    repo: Path,
    subject: str,
    dest: Path,
    providers: list[str],
    log_dir: Path,
    run_id: str,
    timeout_seconds: float,
) -> Path | None:
    """Generate announcement art with the first provider that delivers a PNG.

    Tries providers in order (agy, then codex, then muse); verifies the output
    file's magic bytes rather than trusting the agent's reply. Returns the
    destination path on success, None when every provider fails.
    """
    prompt = render_template(
        IMAGE_PROMPT_TEMPLATE, {"dest": str(dest), "subject": subject.strip() or "(no subject)"}
    )
    for index, provider in enumerate(providers):
        dest.unlink(missing_ok=True)
        result = run_agent(
            provider,
            prompt,
            cwd=repo,
            log_path=log_dir / f"local-image-{_safe_run_id(run_id)}-{index}.log",
            timeout_seconds=timeout_seconds,
        )
        if result.returncode == 0 and not result.timed_out and is_png_image(dest):
            return dest
    dest.unlink(missing_ok=True)
    return None


def recommend_channels(
    drafts: WriterResult,
    *,
    enabled: Collection[str] = ("bluesky", "discord", "github_discussions"),
) -> list[str]:
    """Recommend channels that have generated copy and are enabled."""
    content = {
        "bluesky": bool(drafts.bluesky),
        # Discord uses the corresponding Bluesky message with hashtags
        # removed; it does not need a separate writer draft.
        "discord": bool(drafts.bluesky) or bool(drafts.discord and drafts.discord.strip()),
        "github_discussions": bool(drafts.github_discussions),
    }
    return [name for name in content if name in enabled and content[name]]


def select_forms(
    evaluator: EvaluatorResult, *, enabled: Collection[str] = ("short", "long")
) -> list[str]:
    """Decide which writer passes to run from the evaluator signal.

    Honors explicit channel recommendations; an empty or unrecognized channel
    signal fails open to every enabled form rather than dropping the release.
    """
    if not evaluator.postworthy:
        return []
    allowed = [form for form in ("short", "long") if form in enabled]
    channels = {str(channel).strip().lower() for channel in evaluator.recommended_channels}
    worthy = any(
        isinstance(feature, dict) and bool(feature.get("bluesky_worthy"))
        for feature in evaluator.features
    )
    signaled = [
        form
        for form, wanted in (
            ("short", "bluesky" in channels or "discord" in channels or worthy),
            ("long", "github_discussions" in channels or "reddit" in channels),
        )
        if wanted
    ]
    if not signaled:
        return allowed
    return [form for form in signaled if form in allowed]


def _safe_run_id(run_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "-", run_id)


def _run_pass(
    *,
    repo: Path,
    prompt: str,
    providers: list[str],
    log_dir: Path,
    run_id: str,
    kind: str,
    timeout_seconds: float,
) -> str:
    """Run providers in order and return the agent log output."""
    log_path = log_dir / f"local-{kind}-{_safe_run_id(run_id)}.log"
    log_path.unlink(missing_ok=True)
    accepted = run_with_fallback(
        providers,
        lambda provider: run_agent(
            provider,
            prompt,
            cwd=repo,
            log_path=log_path,
            timeout_seconds=timeout_seconds,
        ),
    )
    if accepted is None:
        raise RuntimeError(f"{kind} pass failed for run {run_id}; see {log_path}")
    return log_path.read_text(encoding="utf-8", errors="replace")


def _delta_context(delta: ReleaseDelta) -> dict[str, str]:
    return {
        "new_sha": delta.new_sha,
        "previous_sha": delta.previous_sha or "(none)",
        "commits": "\n".join(delta.commits) or "(none)",
        "files_changed": "\n".join(delta.files_changed) or "(none)",
        "diff_stat": delta.diff_stat or "(none)",
    }


def run_evaluator_pass(
    *,
    repo: Path,
    delta: ReleaseDelta,
    providers: list[str],
    log_dir: Path,
    run_id: str,
    timeout_seconds: float,
) -> EvaluatorResult:
    """Run the evaluator agent pass and return its validated result."""
    prompt = render_template(load_skill_prompt("release-evaluate"), _delta_context(delta))
    output = _run_pass(
        repo=repo,
        prompt=prompt,
        providers=providers,
        log_dir=log_dir,
        run_id=run_id,
        kind="eval",
        timeout_seconds=timeout_seconds,
    )
    result = parse_evaluator_result(extract_json_block(output))
    if result is None:
        raise RuntimeError(f"evaluator produced no usable result for run {run_id}")
    return result


def _writer_context(evaluator: EvaluatorResult, delta: ReleaseDelta) -> dict[str, str]:
    features = "\n".join(
        f"- {item.get('name', '')}: {item.get('why_users_care', '')}" for item in evaluator.features
    )
    return {
        **_delta_context(delta),
        "reason": evaluator.reason,
        "importance": evaluator.importance,
        "features": features or "(none listed)",
    }


def run_shortform_writer_pass(
    *,
    repo: Path,
    evaluator: EvaluatorResult,
    delta: ReleaseDelta,
    providers: list[str],
    log_dir: Path,
    run_id: str,
    timeout_seconds: float,
) -> list[dict[str, str]]:
    """Run the shortform (Bluesky) writer skill pass."""
    context = _writer_context(evaluator, delta)
    known = load_asset_inventory(repo)
    context["known_assets"] = "\n".join(f"- {key}" for key in known) or "(none listed)"
    prompt = render_template(load_skill_prompt("release-shortform"), context)
    output = _run_pass(
        repo=repo,
        prompt=prompt,
        providers=providers,
        log_dir=log_dir,
        run_id=run_id,
        kind="writer-short",
        timeout_seconds=timeout_seconds,
    )
    result = parse_writer_result(extract_json_block(output))
    if result is None:
        raise RuntimeError(f"shortform writer produced no usable drafts for run {run_id}")
    return result.bluesky


def run_longform_writer_pass(
    *,
    repo: Path,
    evaluator: EvaluatorResult,
    delta: ReleaseDelta,
    providers: list[str],
    log_dir: Path,
    run_id: str,
    timeout_seconds: float,
) -> list[dict[str, str]]:
    """Run the longform (discussion) writer skill pass."""
    prompt = render_template(
        load_skill_prompt("release-longform"), _writer_context(evaluator, delta)
    )
    output = _run_pass(
        repo=repo,
        prompt=prompt,
        providers=providers,
        log_dir=log_dir,
        run_id=run_id,
        kind="writer-long",
        timeout_seconds=timeout_seconds,
    )
    result = parse_writer_result(extract_json_block(output))
    if result is None:
        raise RuntimeError(f"longform writer produced no usable drafts for run {run_id}")
    return result.github_discussions


def run_writer_pass(
    *,
    repo: Path,
    evaluator: EvaluatorResult,
    delta: ReleaseDelta,
    providers: list[str],
    log_dir: Path,
    run_id: str,
    timeout_seconds: float,
) -> WriterResult:
    """Run both writer skills; Discord is derived when the message is published."""
    bluesky = run_shortform_writer_pass(
        repo=repo,
        evaluator=evaluator,
        delta=delta,
        providers=providers,
        log_dir=log_dir,
        run_id=run_id,
        timeout_seconds=timeout_seconds,
    )
    discussions = run_longform_writer_pass(
        repo=repo,
        evaluator=evaluator,
        delta=delta,
        providers=providers,
        log_dir=log_dir,
        run_id=run_id,
        timeout_seconds=timeout_seconds,
    )
    return WriterResult(
        bluesky=bluesky,
        github_discussions=discussions,
    )

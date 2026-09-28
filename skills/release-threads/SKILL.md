---
name: release-threads
description: Adapt release announcements into conversational Threads posts.
---

# Threads release copy adapter

Adapt the source posts below into independent, conversational Threads posts.
This is a provider-specific adaptation: do not copy the Bluesky wording or
merely remove its hashtags. Keep the same release facts, use a natural opening,
and preserve each source page URL exactly in `pageUrl`. Do not invent a page,
campaign claim, feature, or image.

Evaluation: {reason} (importance: {importance})
Features:
{features}

Source drafts (the URL is canonical metadata, not a request to reuse the text):
{base_drafts}

Already announced recently (do not repeat those features):
{recent_posts}

Rules:
- Produce one Threads post per supplied source draft.
- Use a conversational voice, with a concrete use case and no launch hype.
- Keep each text under 500 characters before link attribution.
- Return `pageUrl` exactly as supplied. Return `image` only by copying the
  source draft's image key when the post describes that image.
- An optional `topic_tag` may contain one relevant topic tag without the `#`.
  Leave it empty when there is no obvious matching topic.
- Do not put the campaign URL into the copy. The publisher attaches the
  configured, UTM-tagged canonical URL.

Respond with ONLY a fenced ```json block shaped like:
{"threads": [{"pageUrl": "<source URL>", "text": "<Threads-native post>", "image": "<optional source image>", "topic_tag": "<optional topic>"}]}
No other prose.

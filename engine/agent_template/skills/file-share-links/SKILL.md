---
name: file-share-links
description: Load whenever a file must reach anything outside this container - a remote MCP tool such as Zapier, a webhook, an HTTP API, an email sent by a remote service, or a user who needs a download link. Creates a temporary download URL for a workspace file.
---

# File Share Links

## Why
Remote tools run outside this container and cannot read `/home/node/custom-state/...` paths. If you pass a path, the tool attaches the path text (for example an email whose attachment only contains the path). Pass a URL instead and let the tool fetch the file.

## How
Request the link right before the tool call:

```bash
curl -sS --fail-with-body -X POST "$MARACLAW_API_BASE/api/gateway/file-links" \
  -H "X-Api-Key: $MARACLAW_GATEWAY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"path":"workspace/report.docx"}'
```

The response is JSON: `{url, expires_at, expires_in_seconds, filename, size_bytes}`. Put `url` into the remote tool's file, attachment or URL field (for example the Zapier Gmail `file` and attachment fields accept a URL to fetch).

## Rules
- Only files under `workspace/` or `media/outbound/` can be shared.
- The link is public to anyone holding it until `expires_at` (15 minutes by default). Share only files the user asked you to send, and never post a link publicly.
- Request the link right before the tool call, not earlier.
- Never print, echo or log `$MARACLAW_GATEWAY_API_KEY`.
- On an error status, report it to the user. Never fall back to sending the local path.
- After sending, check that the tool's result reports an attachment with the expected file name and size before telling the user it was attached.

## Errors
- 400: the path cannot be shared (outside `workspace/` and `media/outbound/`, or not a plain file path).
- 404: the file does not exist.
- 409 `file_conflict`: a newer version from the web app replaced it; your copy was saved under `workspace/conflicts/`. Tell the user and ask which version to send.
- 409 `openclaw_update_in_progress` or 423 `transfer_cleanup_pending`: the workspace is busy with an update or a company transfer. Retry later.
- 413 or 507: storage limits (file too large or storage full). Tell the user.
- 503: temporarily unavailable. Retry later.

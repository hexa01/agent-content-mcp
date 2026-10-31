# Changelog

All notable changes to agent-content-mcp. Versions follow [semantic versioning](https://semver.org/).

## 0.2.1 (2026-10-31)

### Fixed
- Every tool call failed with "Internal server error" in 0.2.0 (the server raised "cannot access local variable
  'problem'"). Tool calls work again. If you installed 0.2.0, update: `uv cache clean agent-content-mcp`, then
  restart your MCP client.

### Added
- Tests (`python -m unittest`) for tool building, tool calls, uploads and refused files, run against a fake site.

## 0.2.0 (2026-10-30)

### Added
- Upload tools for file and image fields the site allows uploads to: `upload_<item>_<field>` (for example
  `upload_post_cover`), `upload_<name>_<field>` for single-object resources, and a `<field>_path` argument on
  `create_<item>` when an item needs its file from the start.
- Uploads take a path to a file on your computer and are checked before anything is sent: the type must be one the
  field accepts, the size within its limit, and hidden files or anything in a hidden folder (`~/.ssh`, `.env`) are
  never sent.

### Requires
- django-agent-content 0.2.0 or later on the site for uploads. Sites on 0.1.x keep working, without upload tools.

## 0.1.0 (2026-10-15)

First release: an MCP server whose tools (list, get, create, update, actions and inline lists) are generated from a
django-agent-content site's API description.

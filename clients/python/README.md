# Nebula terminal client

A small Python 3.11–3.13 client for an already configured Nebula Core. All models,
tools, skills, goals and running work stay on Core. Linux and macOS are supported.

Install the `nebula_client` wheel from the matching Nebula release with:

```console
pipx install /path/to/nebula_client-VERSION-py3-none-any.whl
nebula-chat --core https://nebula.example:8765 pair
nebula-chat
```

Ask the Core host to issue a terminal invitation with `nebula-core chat invite`.
Pairing asks for its secret and confirmation code through hidden prompts.
For a private certificate authority, add `pair --ca-cert /path/to/ca.pem`.

On an interactive terminal, the full-screen Assistant shows projects and chats,
a saved transcript, live activity, and a message composer. F2/F3 browse projects
and conversations; F4 opens a searchable command menu. Type `/help` for all
commands. Enter sends; Esc followed by Enter adds a line. Ctrl-C stops a
supported selected turn; Ctrl-D or `/quit` leaves work running on Core.
Set `NEBULA_CHAT_PLAIN=1` for the scrolling prompt and numbered menus.

Open `/board` (also in F4 Commands) to read the selected project's Work board.
Up/Down selects a task; Tab focuses its description and recent check-ins.
Use `/` to search, R to refresh, and Escape to return to the same chat and draft.
The board is read-only and does not poll; plain mode prints tasks grouped by status.

The Assistant shell requires Core terminal protocol 1. The legacy
`nebula-chat chat --project "Project"` Butler command also works with older Cores.
The client stores only pairing credentials and selected identifiers locally.
Use `nebula-chat unpair` to revoke this terminal.

The source distribution is self-contained. From the main repository, build both
artifacts with `python -m scripts.build_terminal_client`. This stages only the
allowlisted client source and uses this independent build configuration; it does
not install or build Core, download models, or need Node/Rust/browser libraries.

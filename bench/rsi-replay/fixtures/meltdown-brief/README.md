# Tidepool

Tidepool keeps a folder of Markdown notes in sync across machines. It is a small CLI, not a service.

## Use

```
tide init ~/notes
tide sync
```

`tide sync` prints one status line, for example:

```
3 notes synced, 0 conflicts
```

A conflict is never merged silently. Tidepool writes both versions next to each other
(`plan.md` and `plan.conflict.md`) and tells you.

## Facts

- Written in Go, one binary, 4.1 MB.
- Notes stay plain Markdown files. No database, no account.
- Sync goes over SSH to any machine you already reach.
- The logo is three rubber ducks in a tide pool. Nobody remembers why.

## Status

Version 0.9.2. Used daily by its author on two laptops and a Raspberry Pi.

# Klearing — project context for Claude

Full product context lives in `PRODUCT.md` at the repo root. Read that
first for what Klearing is, who it's for, and its register.

## Skills

Design constitution: skills/klearing-ui/SKILL.md
Section workflow: skills/klearing-section/SKILL.md

Load the design constitution before touching `ui.py`, `charts.py`, or any
render function in `app.py`. Load the section workflow when asked to run
a full improvement cycle on a specific screen — it wraps
`SECTION_PLAYBOOK.md`'s phase checklist with the design constitution and
the competitor-benchmark research already gathered.

## Context folder

`.context/` is a portable snapshot of all project context files (skills,
playbook, product doc, CLAUDE.md) for dropping into any AI tool. Run
`python .context/sync.py` whenever skill files or context files are
updated to keep `.context/` current. `.context/CONTEXT.md` is
hand-authored — edit it directly if the high-level overview changes.

## Testing

Port 8642 is the live running Klearing instance — always use an isolated
port and separate data-dir for testing, never probe 8642 from automated
scripts.

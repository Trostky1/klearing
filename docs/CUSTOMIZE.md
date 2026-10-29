# Make Klearing yours

Klearing is free software that you can shape to your business. This page lists
what you can change, from easy to advanced.

## Without code, in Settings

Open **Settings, General**. You can change:

- **Alerts.** Set a low cash balance, a runway in days, and how big a vendor
  spend jump must be before Klearing flags it.
- **Alert email.** Where alerts go.
- **Automatic category suggestions.** Turn them on or off.
- **AI assistant.** Use a model on your own machine (Ollama, the default) or an
  optional cloud provider. Klearing warns you before any financial data leaves
  your machine.
- **Home insights.** Choose which numbers show on Home.
- **Local businesses.** Keep more than one business in one install. Each has its
  own books.
- **Backup.** Make an encrypted backup file and restore from it.

Open **Settings, Connections** to turn services on or off. Each one is optional.

## Make the screens fit how you work

- Press **Ctrl K** (Cmd K on a Mac) to jump to any page.
- Use the **New** button to start an invoice, a recurring invoice, or a contact.
- On **Contacts, Pipeline**, set a stage on each contact to see your customers
  in columns.
- On **Review**, pick several rows and handle them together.

## With a little Python

Klearing is plain Python and plain HTML. There is no build step. Change a file,
reload the page.

| I want to... | Look at |
|---|---|
| Add a bank | [BANK-ADAPTERS.md](BANK-ADAPTERS.md). One file, under 150 lines. |
| Change a color or spacing | The `:root` block at the top of `ui.py`. Use tokens, not raw colors. |
| Change a page | The `render_*_page` function in `app.py`. |
| Change the sidebar | `NAV_GROUPS` in `ui.py`. |
| Add a command to the search box | `PALETTE_ENTRIES` in `ui.py`. |
| Add a chart | Extend one of the five chart types in `charts.py`. |

Dev setup, style, and the pull request flow are in
[../CONTRIBUTING.md](../CONTRIBUTING.md).

## House rules when you change things

These keep the books trustworthy. They are not optional.

- Money is whole cents. Never use floating point for stored money.
- A new way to change data uses the same pattern as the others: a form posts,
  Klearing answers with a redirect, and a banner shows the result.
- A confirmation uses the page's own dialog, not a browser pop-up.
- Run the regression scripts in `scripts/` before you share a change.
- Do not add a cloud service. Klearing runs on your machine.

## Use a test copy first

Never try changes on your real books. Point Klearing at an empty folder and a
different port:

```bash
KLEARING_DATA_DIR=some/empty/folder KLEARING_PORT=8791 venv/Scripts/python main.py --serve
```

Open http://127.0.0.1:8791. Port 8642 is your real copy. Leave it alone.

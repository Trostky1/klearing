"""Small command-line maintenance tools for Klearing."""

import argparse
import time

import db
from src import categorize as categorizer


def recategorize_pending():
    """Fill missing suggestions without changing any item status."""
    db.init_db()
    items = [item for item in db.list_items(status="pending")
             if not (item.get("suggested_category") or "").strip()]
    total = len(items)
    for index, item in enumerate(items, start=1):
        category, confidence, source = categorizer.categorize(
            item["vendor"], item["amount"], item["direction"], db
        )
        db.set_category_suggestion(
            item["id"], category, confidence if category else None,
            source if category else None,
        )
        print(f"Categorized {index} of {total} pending items")
        if source in ("ollama", "timeout") and index < total:
            time.sleep(1)
    return total


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cli.py")
    commands = parser.add_subparsers(dest="command")
    recategorize = commands.add_parser("recategorize")
    recategorize.add_argument("--pending", action="store_true")
    args = parser.parse_args(argv)
    if args.command != "recategorize" or not args.pending:
        parser.error("use: cli.py recategorize --pending")
    recategorize_pending()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Sync .context/ from source files.
Run this whenever any source file changes:  python .context/sync.py
"""

import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONTEXT = Path(__file__).resolve().parent

COPIES = [
    (REPO / "CLAUDE.md",                                                  CONTEXT / "CLAUDE.md"),
    (REPO / "PRODUCT.md",                                                 CONTEXT / "PRODUCT.md"),
    (REPO / "SECTION_PLAYBOOK.md",                                        CONTEXT / "SECTION_PLAYBOOK.md"),
    (REPO / "skills/klearing-ui/SKILL.md",                               CONTEXT / "skills/klearing-ui.md"),
    (REPO / "skills/klearing-ui/references/design-tokens.md",            CONTEXT / "skills/design-tokens.md"),
    (REPO / "skills/klearing-ui/references/chart-patterns.md",           CONTEXT / "skills/chart-patterns.md"),
    (REPO / "skills/klearing-ui/references/anti-patterns.md",            CONTEXT / "skills/anti-patterns.md"),
    (REPO / "skills/klearing-section/SKILL.md",                          CONTEXT / "skills/klearing-section.md"),
    (REPO / "skills/klearing-section/references/section-inventory.md",   CONTEXT / "skills/section-inventory.md"),
    (REPO / "skills/klearing-section/references/competitors.md",         CONTEXT / "skills/competitors.md"),
    (REPO / "skills/klearing-financial-ui.md",                           CONTEXT / "skills/klearing-financial-ui.md"),
]

synced = 0
for src, dst in COPIES:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"  synced  {src.relative_to(REPO)}  ->  {dst.relative_to(CONTEXT.parent)}")
    synced += 1

print(f"\nSynced {synced} files")
print("Note: .context/CONTEXT.md is hand-authored — edit it directly if the overview changes.")

# Bundled fonts

Klearing ships its own typefaces so it looks identical on every machine
and works with no network. No CDN, no build step — these are served by
route from `app.py` exactly like `frontend/tax/*.js`, and bundled into
the binary by `build/klearing.spec`.

## Files present

| File | Family | Licence | Role |
|---|---|---|---|
| `instrument-sans.woff2` | Instrument Sans, variable 400–700 (~29KB) | OFL 1.1 | All interface text |
| `instrument-serif.woff2` | Instrument Serif, regular 400 (~21KB) | OFL 1.1 | Document register only — statement titles, Home briefing, export cover |
| `commit-mono.woff2` | Commit Mono, static regular 400 (~47KB) | OFL 1.1 | Numeric table columns, codes, timestamps, logs |
| `commit-mono-bold.woff2` | Commit Mono, static bold 700 (~46KB) | OFL 1.1 | Same, bold weight |

The two Google Fonts files are Google's own Latin-range split
(`U+0000-00FF` plus the standard punctuation/currency extras), fetched
directly from `fonts.gstatic.com` — already the smallest correctly-hinted
build Google serves, so there is no further subsetting step.

**Commit Mono is not subset and is not variable.** Eigil Nikolajsen
distributes it as static per-weight OTF files with no variable build and
no pre-subset option (github.com/eigilnikolajsen/commit-mono, v1.143).
The two files here are the stock 400 and 700 OTFs converted to woff2 with
`fontTools.ttLib` (`font.flavor = "woff2"; font.save(...)`) and nothing
else changed. `ui.py` declares two separate `@font-face` blocks for it —
one `@font-face` per weight, not one range — because that split-by-weight
shape is what a static font actually is; treating it as a `400 700` range
block would only render correctly at one weight.

Each family's licence text ships beside it as `<family>-OFL.txt` —
required for commercial distribution. Commit Mono's licence file is
copied verbatim from its release archive; the two Google Fonts licences
are Google's canonical OFL text for each family
(`github.com/google/fonts/ofl/<family>/OFL.txt`).

## If a file is missing

Nothing breaks. `--font-ui`, `--font-doc` and `--font-mono` in `ui.py`
each carry a full fallback stack, and every `@font-face` uses
`font-display: swap`, so a missing file degrades to the previous system
stack rather than blanking text. The app will simply look like it did
before this redesign.

## Sources

- Instrument Sans / Instrument Serif — Google Fonts, by Instrument.
- Commit Mono — commitmono.com, by Eigil Nikolajsen.

All three are SIL Open Font License 1.1, which permits bundling in a
commercial application provided the licence ships with them and the
fonts are not sold on their own.

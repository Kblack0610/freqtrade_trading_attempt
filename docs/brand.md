# Brand

Trader is internal: one operator, no users, no store listing, no marketing. There is no logo or brand to protect. This sheet exists so the site looks the same everywhere and so a new page does not invent colours.

## Palette

The shell (fonts, type scale, ink, muted, line, accent, sidebar) comes from the pinned dotfiles web-kit (`web/kit.lock`, fetched into `web/static/kit/`). Trader adds only the colours below, in `web/static/style.css`.

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| `--up` | `#1a7f4b` | `#4cc38a` | Profit, passing gates |
| `--down` | `#c0392b` | `#ff6b5e` | Loss, failing gates |
| `--alert-bg` | `#fdecea` | `#3a1d1a` | Background of an alert row |

Dark mode follows `prefers-color-scheme`. Green and red always carry a sign or a word as well, so the page reads without color.

## Logo

None. The site ships no icon or favicon of its own. Nothing here is sent to users, so a logo would serve no one.

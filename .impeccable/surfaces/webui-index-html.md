---
version: 1
slug: "webui-index-html"
primary_target: "webui/index.html"
related_targets: ["webui/style.css","webui/app.js"]
---

# Local management console

Mode: Operate. Scope: webui/index.html, webui/style.css, webui/app.js.
The user confirmed the existing workflow and delegated priority and visual decisions. This is a code-led app redesign; no additional direction approval is needed under that delegation.

## Direction exploration

Grounded systems, ordered by resonance: 1. a Chinese appliance service booklet (direct task labeling); 2. a public-library wayfinding program (quiet navigation hierarchy); 3. a contemporary correspondence directory (consistent identity rows, usable state columns); 4. a modular exhibition checklist (legible dense records); 5. a neighborhood noticeboard identity (clear local scope); 6. a transport publication's route index (status vocabulary); 7. a stationery filing system (stable sections and practical forms).

The assigned third direction is selected: a correspondence directory. In the user's daily scene of checking local robots on a Windows desktop, a light reading surface, muted green navigation shell and dark ink support repeated work without glare. Standard web navigation and controls remain; the directory contributes typography, palette, compact row density, and one signature: each robot has a real identity row with a stable state and a direct management action.

Challenger verdicts: split-flap board declined (less personal and less readable; retain rigorous state-column alignment); Metro competitive on user familiarity but weaker on simultaneous status clarity (retain confident spacing, not tiles); Crouwel declined (construction display obscures records; retain systematic alignment); Labanotation declined (unfamiliar reading direction; retain explicit parallel-state distinction); botanical folio declined (image dependency mismatches operation; retain comparable state treatment); catalog sleeve declined (coded labels weaken clarity; retain disciplined removal of decoration). No catalog artwork or type is copied.

## Direction contract

THESIS: Identity before decoration. Show which robot is selected, what it is doing, and the next available action. Replace the oversized illustration and uniform card wall with a working directory and structured settings.

OWN-WORLD: White task surfaces, muted green navigation ground, forest-green active controls, dark green-black text, readable Chinese sans, restrained rules, consistent native buttons and fields. No decorative glyph tiles, English kickers, soft hero artwork, or invented charts.

STORY: The operator selects logged-in WeChat accounts and explicitly starts the selection, then manages an account's plugins, groups, models or logs. Profiles are matched automatically; existing configuration remains intact.

FIRST VIEWPORT: A stable left navigation rail and top account selector frame the workspace. A full-width logged-account list leads, with checkboxes, identity, real state and a batch-start action. Each row directly starts or stops its account; omit the separate current-account status panel. Plugins use aligned rows rather than equal-weight tiles.

FORM: Correspondence directory, third grounded direction; seed:fe67cb7dd1b3dffe. The signature identity/state row persists across status, directory and plugin lists. On phones the navigation becomes a labeled drawer and rows reflow without losing state or controls.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## User-directed refinement

Remove repeated explanatory copy, brand subtitle, client-version badge and local-address/status copy. Keep functional labels and necessary error or configuration feedback. Add an accessible light/dark switch in the toolbar, remember the explicit browser preference and follow the system before a choice is made. Both themes retain the directory layout and existing bot behavior.

Dark-theme correction: the user specifically requested neutral black/gray, with ChatGPT as a reference. Use a black navigation shell, charcoal canvas and task surfaces, gray selection, and neutral controls. The light palette remains unchanged. Success, warning and error retain semantic color only.

Account workflow refinement: replace manual profile creation and binding with discovered accounts, explicit checkbox selection, and one batch-start action. Selecting an account in the top selector prepares its isolated configuration without starting it; row actions do not switch the configuration view. Keep account selection distinct from the current management object, retain focus during polling, and show per-account failures. No live service or bot is launched during verification; synthetic browser routes cover desktop/mobile, both themes, offline, empty, stale-selection and partial-failure states.

User-directed simplification: remove the entire selected-account status panel and its summary metrics. Replace each row management button with start/stop, keeping batch start and the top configuration selector.

Message-monitor refinement: replace overview shortcuts with a bounded, per-account live feed showing local time, conversation ID, sender identity and plain content. Use media type labels only. Preserve both themes, table semantics, mobile record reflow, search, pause-scroll and local clear. Account switches discard old responses; monitor failures cannot disable lifecycle controls.

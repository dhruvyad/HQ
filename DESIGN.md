# HQ design guidelines

HQ uses NoClick's black/zinc/white product language with a single, focused task
queue. Keep the interface short, readable, and centred on the next action.

## Evidence

Requested reference: https://staging.noclick.com/ (2026-10-14). The public web
reader could not access staging, and browser inspection is blocked by an
admin-policy verification failure. No staging screenshot was captured. These
rules are observed in NoClick's local source; parity with the deployed staging
version remains unverified.

Local reference checkout at commit `6a87b1894fae8e9cff56ba6eb142441afb8194c8`:

- `noclick/frontend/app/tailwind.css`: exact semantic dark tokens.
- `noclick/CLAUDE.md`, Theming: three black surface levels, semantic colours.
- `noclick/frontend/tailwind.config.ts` and `app/root.tsx`: Inter and Outfit.
- `noclick/frontend/app/components/dashboard/primitives.tsx`: black/zinc/white,
  quiet surfaces, hairline dividers, restrained row hovers; red for errors.
- `noclick/frontend/app/components/nav/NavBar.tsx` and `ui/tabs.tsx`: compact
  rounded tabs and counts.
- `noclick/frontend/app/components/ui/button.tsx`: inverted white primary action,
  outline/ghost secondary actions, small rounded controls.

NoClick files are read-only references for this change. Dashboard structure and
spacing below are HQ adaptations, not measurements of the deployed site.

## Tokens

Use semantic variables in `hq/static/style.css`; do not scatter literal colours.
The source HSL values are retained so updates can be compared directly.

| Role | Value | Use |
| --- | --- | --- |
| Background | `#000000` | Page and header |
| Sunken | `#09090b` | Conversation rail and recessed fields |
| Card | `#18181b` | Raised controls and reply surfaces |
| Popover | `#1a1a1a` | Dialogs and detail panel |
| Foreground | `#fafafa` | Titles and primary actions |
| Muted foreground | approximately `#a1a1aa` | Supporting text |
| Border | approximately `#27272a` | Inputs and boundaries |
| Quiet surface | foreground at 2% | Conversation group |
| Hairline | foreground at 8% | Group boundary |
| Hover | foreground at 3% | Item rows |
| Error | red-400 (`#f87171`) | Failed delivery/errors only |

Success and pending states use neutral text/icons. Keep colours out of general
navigation and workstream tags. Preserve separate idle, selected, and hover fills.

## Typography and layout

Inter Variable is the interface font; Outfit Variable is limited to the HQ mark,
page title, and detail title. Both are bundled locally with their SIL OFL licences.
No font requests leave the laptop. Use system fallbacks and `font-display: swap`.

HQ uses readable 14px body/row titles, 12px supporting text, and 11px compact
badges. Use a 4px spacing rhythm, 6px control corners, 12px group corners, a
56px header, a 248px conversation rail, and 24–28px main padding. These dimensions
are adaptations for HQ, not copied from staging. Main content tops out at 1200px.
The detail drawer stays a single column, up to 560px wide.

## Components and content

- Conversations are the primary navigation. Show conversations with unfinished
  items, with a quiet harness badge and item count. Hide discovered chats with no
  reported work. Completed conversations appear in Done; keep the selected
  conversation visible when its last item completes.
- Tags are optional context in item details and remain searchable. Keep tag
  lists and tag pills out of the sidebar and queue rows.
- Use compact rounded state tabs with counts; keep Human pending and Agent
  pending visible. Waiting, Later, and Done remain accessible on small screens.
- Conversations are quiet rounded groups with hairlines between items. Titles
  carry the emphasis; badges and status are secondary. One item is one action.
- Use a white primary button only for the main action. Connection instructions,
  cancellation, copying, and moving an item use quieter controls.
- Context, choices, steps, copy blocks, and per-item clarification threads stay
  in the detail drawer. Agent responses must preserve typed form drafts.
- Show only information that helps the human decide or act. No greetings,
  decorative dates, marketing copy, global statistics, or duplicate navigation.
- On narrow screens, hide the rail behind the existing menu, stack the toolbar,
  keep tabs scrollable, and fit the drawer to the viewport. Respect reduced
  motion and provide clear keyboard focus. Errors always include text.

## Validation boundary

Source, HTTP assets, pure rendering logic, and existing task lifecycles are
checked locally. Browser visual inspection and staging comparison remain in HQ
as pending work until authorised browser access is available.

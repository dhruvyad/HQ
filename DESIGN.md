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

Inter Variable is the interface font. Instrument Serif (one weight, never faux
bold) sets page and dialog titles, as NoClick's app shell does; Outfit is the HQ
wordmark. All are bundled locally with their SIL OFL licences.

Rows are 15px, supporting text 13px. The sidebar is 264px of pure black with
40px rounded rows (15px) and 36px workstream rows on a guide line. Page content
is centred at up to 880px with 34px serif titles. The item pane is 500px on the
right; under 1180px it floats over the list, and under 760px the sidebar becomes
a sheet and the pane fills the screen.

## Components and content

- Needs you, Agents and Projects are the only navigation. A project expands into
  its workstreams only while you are in it, so the sidebar stays short.
- Group lists by spacing, not rules: a quiet header (project › workstream), then
  one-line rows with the title and at most two short facts (Decision · 3 min).
- One pill-tab set per scope: Needs you, With agents, Waiting, Later, Done.
- The item pane leads with the action: state line, title, numbered steps that
  carry their link, copy text or checks, then Looks right / Something was off,
  a quiet "Not now?" row, Why it matters, Why you, not the agent, and a
  collapsed More context. Drafts, ticks and form fields survive live updates.
- Activity labels distinguish Active, Idle, Waiting, Offline and Unknown; green
  marks only an active agent, red only failures and overdue dates.
- Use a white primary button only for the main action. No decorative icons on
  rows, no tag pills in lists, no global statistics beyond the Needs you line.
- Respect reduced motion; keyboard focus is always visible; errors carry text.

## Floating panel

A native window around the local page `/float`, in three sizes of one surface. The window
always hugs its content, so nothing transparent sits over what you are working on.

- **Pill** a pure black capsule of 28px circles stacked vertically, each a live number you
  choose and order. No wordmark. The count you act on is white when it is above zero, an
  active agent is the only green, and usage is a ring showing the tightest limit, red at
  10% or less. A circle with no source is dashed and shows a dash. Hover reveals labels,
  opening away from the screen edge so the circles under the cursor stay put. It never
  takes keyboard focus. New items pulse the first circle once.
- **List** 380px wide, hugging its rows up to 520px: a serif "Needs you" and one-line-title
  rows that say what kind of thing each is, where it lives and its age. Pinned and due-soon
  items lead, then newest. A reply that failed to deliver appears here too.
- **Item** 480px wide, hugging its content up to 760px: the dashboard's item pane, reused and
  never re-implemented. Answering moves on to the next item, and the toast says the reply was
  saved rather than received, since only an agent acknowledgement can claim that.
- **Drag** follows OpenFlow's flow bar: a 5px threshold separates a click from a drag, then
  the window covers the display, the screen dims, dashed targets appear, the nearest fills in,
  a ghost capsule follows the cursor and glides 300ms into the drop. The list and item headers
  drag the same way. Escape cancels. Eight targets; the top-left one stays 74px clear of a
  maximized window's traffic lights. The box always grows away from its dock.
- Sunken surface, 16px radius, a native shadow, and a 14% hairline, so it reads on any wallpaper.

These are the starting points of `hq float lab`, which flips each one live on the real
window. Settle a round there, then update this section with the picks.

## Validation boundary

Source, HTTP assets, pure rendering logic, and existing task lifecycles are
checked locally. Browser visual inspection and staging comparison remain in HQ
as pending work until authorised browser access is available.

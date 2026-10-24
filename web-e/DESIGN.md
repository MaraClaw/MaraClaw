# MaraClaw member workspace design

Extracted from `src/index.css`, the UI primitives and the existing agent Settings and Inbox. This contract preserves the current design; it does not introduce new tokens.

## 1. Atmosphere & identity
Warm paper surfaces, compact operational controls and Instrument Sans headings. Member workflows remain inside AppShell and AgentLayout, without marketing motion or extra navigation.

## 2. Color
`src/index.css` owns the light/dark OKLCH values. Use `background`/`foreground` for the workspace, `card`/`card-foreground` for panels, `muted-foreground` for supporting text, `border` for neutral hairlines, `destructive` for errors and `primary`/`primary-foreground` for the main action. `accent` is the existing warm tonal surface. No new palette values.

## 3. Typography
Body: Inter with system fallback. Headings: `font-display` (Instrument Sans). Workspace page titles use `text-xl` or `text-2xl` with semibold weight; card titles `text-base`; supporting body `text-sm` with relaxed leading; metadata `text-xs`. Long notices retain line breaks and wrap unbroken strings.

## 4. Spacing & layout
Use Tailwind's existing 4px spacing scale: gaps 2–4, page/card padding 6. Settings remains `max-w-xl`; notice detail uses the Inbox `max-w-2xl` column. AgentLayout retains the workspace and agent section rails. Chat (default, shared-session and archive URLs) fills all remaining width and height, with no Back row, Conversations list or session-management sidebar. Non-chat tabs retain Back above their scrolling panel. On phones the rails remain top strips; chat sits directly below them without an empty header track. Small screens stack actions and keep controls at least 44px high in the new flow.

Chat follows the [scroll-body-shell](https://github.com/changeroa/StyleGallery/blob/main/patterns/viewport-shell/scroll-body-shell.md) containment pattern: bounded `min-h-0`/`min-w-0` ancestors and shrinking grid tracks, a thread that owns vertical scrolling, and a composer anchored at the bottom of the available panel. Message bubbles retain their readable maximum width; the thread and composer are not placed in a narrow centered column. No document-level horizontal overflow at 390/1440/1920px in either theme.

## 5. Components
Reuse Button (primary for Upgrade, outline for navigation/retry), Badge (soft severity labels), Card and its header/content primitives. The shared OpenClaw update card contains a heading, optional/mandatory badge, live status and explicit action. Settings executes Upgrade; notice detail and the work gate link to Settings. Loading/error states never mount agent work. Settings remains reachable through the agent rail, and Back remains on non-chat tabs. Unavailable chat exposes an explicit Agents recovery link rather than recreating the Back row. Use-only members are directed to a manager. Failure retains mandatory gating and exposes retry instructions.

Writable chat starts with the thread, without a shared-conversation access notice. Explicit read-only archive URLs retain the archive banner and Return to shared conversation link. Chat preserves uploads, receipts, socket ownership fencing and Take Control action. Session metadata still determines direct-route permissions even though session browsing, rename and delete controls are absent; shared-session history polling remains unchanged.

Chat attachments use a neutral rounded-2xl card inside the existing message, with a wrapping filename and explicit Preview/Download buttons (44px targets). Preview uses a Radix modal with a titled header, description, Close action and independently scrolling body, inset 4 on phones and capped at max-w-4xl on desktop. Native images fit the viewport; PDFs load on demand and render one navigable, selectable-text page at a time. DOCX/PPTX use one navigable page/slide and selectable text; XLSX keeps a bounded-height scrollable grid with worksheet navigation. Document canvases preserve authored white paper/ink in either app theme. Loading, unavailable, oversized and unsupported states retain Download and Close. Renderers are browser-local; the Files tab is unchanged.

Chat opens at the newest messages. The thread's top uses the existing outline Button for Load earlier messages and retry (44px targets), with muted loading/beginning status and destructive error text. Scrolling near the top loads one earlier page per approach; keyboard users can activate the same action. Prepending retains the visible entry position with native overflow anchoring disabled. Later arrivals follow the bottom only if the reader was already near it; retained earlier pages survive newest refreshes and cached revisits. Earlier-page failures never disable the composer or disconnect chat.

Keyboard-activated earlier loading keeps its button focused with `aria-disabled` and blocked activation until the request settles or focus moves away. Automatic scroll loading uses native disabled behavior and never takes focus from the composer.

## 6. Motion & interaction
Inbox uses labelled Active/Archived pressed buttons above the retained Category select. Switching view restarts at the first page. Archive/Restore are reversible, text-labelled outline actions (44px minimum), with no confirmation dialog. Read state remains separate: unread rows use an accent wash with a neutral hairline; bulk-read and counts cover Active only. Archived rows retain Open and individual Mark as read actions. Live feedback describes moves and errors without taking focus from the user's view selector.

Reuse existing button hover/press/focus states; no new animation. Async actions disable immediately. Status is a polite atomic live region, errors use alert, and unknown-duration updates use words rather than invented percentage progress. An upgrade begins only after an explicit click, never on notice opening or agent start.

## 7. Depth & surface
Reuse Card's rounded-2xl neutral border and `shadow-card`; Button/Input remain rounded-xl. Highlight Upgrade through the existing primary button and warm accent surface, not accent stripes or accent outlines.

## 8. Accessibility constraints & accepted debt
Target WCAG 2.2 AA: keyboard-accessible actions, visible focus, text labels for status, decorative icons hidden, no color-only severity, live feedback, wrapping labels and responsive content. Existing workspace rail/primitives remain unchanged. Chat preserves navigation landmark names, composer/attachment labels and keyboard submission; long threads scroll without moving the rails or composer offscreen. No new design debt is accepted by this feature.

Attachment dialogs trap focus, close with Escape and restore focus to Preview. PDF annotations/forms and Office hyperlinks/media playback are disabled. Office renderers load no external fonts or optional math/3-D/ChartEx modules, enforce archive/worker limits, and destroy owned resources on close or failure. Text selection remains available for PDF/DOCX/PPTX; spreadsheet canvas accessibility is limited to the renderer's grid controls and download fallback. Legacy DOC/XLS/PPT remain download-only. Every reopening checks current access/version before showing session-cached bytes; workers receive disposable copies.

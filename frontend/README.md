# Frontend

React + TypeScript + Vite SPA, served through CloudFront from a private S3 origin.

## Quick start

```bash
nvm use            # Node version pinned in .nvmrc
npm ci             # the lockfile is the sole install authority
npm run dev
```

The repository has three separate npm projects — `frontend/`, `src/relay/` (see
[`docs/relay-artefact.md`](../docs/relay-artefact.md)), and `tests/e2e/browser/` — each with its own
lockfile and no workspace. Run `npm ci` in each one you use.

## Runtime configuration

No account id, Cognito pool id, or endpoint is committed. The app fetches `/config.json` at start
(same-origin under CloudFront) — see `src/config/runtimeConfig.ts`. A deployed site's `config.json` is
rendered from the Terraform outputs by `scripts/render_frontend_config.py`.

`npm run dev` runs against stubs, not a backend: `src/mocks/devServerPlugin.ts` (dev server only)
serves `/config.json`, `POST /api/<tool>` and `GET /api/capabilities` from in-process stubs and seeds a
dev session, so no `public/config.json` is needed. For `npm run build` or `npm run preview` with a
hand-written config, copy `public/config.example.json` to `public/config.json` and fill in the
deployed values. `frontend/public/config.json` is gitignored by the repository-root `.gitignore`;
confirm with `git check-ignore -v frontend/public/config.json`.

| Key | Meaning |
|---|---|
| `cognitoUserPoolId` | Cognito user pool id |
| `cognitoClientId` | Cognito SPA app client id (public, no secret) |
| `cognitoRegion` | Cognito user pool region |
| `cognitoHostedUiDomain` | Hosted UI domain, or `""` for SRP-only auth |
| `apiBaseUrl` | Always `/api` — the CloudFront behaviour to API Gateway |
| `agentBaseUrl` | Always `/agent` — the CloudFront behaviour to the streaming relay's Function URL |
| `agentRuntimeArn` | AgentCore Runtime ARN, raw and not URL-encoded; the client encodes it when building the `/agent/<arn>` path, so a pre-encoded value is encoded twice |

## Scripts

| Script | Purpose |
|---|---|
| `npm run dev` | Vite dev server |
| `npm run build` | `gen:types`, `tsc -b`, then `vite build` to `dist/` |
| `npm run preview` | Serve a built `dist/` locally |
| `npm run gen:types` | Runs `scripts/generate-types.mjs` and `scripts/gen-agent-types.mjs`, regenerating `src/types/generated/`. Requires the repository's `.venv`. |
| `npm run typecheck` | `gen:types` + `tsc -b --noEmit`. Rewrites `src/types/generated/`, so do not run two concurrently. |
| `npm run typecheck:no-gen` | `tsc -b --noEmit` only; writes nothing, so it is safe to run concurrently. Assumes `src/types/generated/` is current. |
| `npm run lint` | ESLint |
| `npm run test` | Vitest, single run |
| `npm run test:watch` | Vitest, watch mode |

## Architecture

### Auth (`src/auth/`)

`AuthProvider` + `useAuth()` wrap Cognito SRP authentication via `CognitoAuthSession`
(`amazon-cognito-identity-js`):

```ts
interface AuthContextValue {
  status: "loading" | "signed-out" | "signed-in";
  identity: Identity | undefined;   // decoded rep_id / book / full_visibility custom claims
  signIn(username: string, password: string): Promise<void>;
  signOut(): void;
  getAccessToken(): Promise<string>; // refreshes silently when expired
}
```

`src/auth/hostedUi.ts` provides the Hosted UI redirect flow, inert unless `cognitoHostedUiDomain` is
set.

### Typed API client (`src/api/`)

`CrmApiClient` has one method per tool in `src/crm/toolspec/definitions.py`, typed against generated
request interfaces. `useCrmApi()` binds a client to the current session. Every call is
`POST {apiBaseUrl}/{tool_name}`, e.g. `POST /api/search_opportunities`. Non-2xx responses become a
`CrmApiError` whose kind is one of `authentication_failed`, `scope_denied`, `not_found`,
`validation_failed`, `conflict`, `unknown`.

### Generated types (`src/types/generated/`)

| Generated file | Generator | Source of truth |
|---|---|---|
| `toolSchemas.ts` | `generate-types.mjs` (via `scripts/export_tool_schemas.py` and `json-schema-to-typescript`) | `src/crm/toolspec/definitions.py` — tool input schemas, plus `TOOL_METADATA` (risk class, eligible agent roles) |
| `agentEvents.ts` | `gen-agent-types.mjs` | `src/agents/event_schema.py` — the SSE frame contract (the relay generates its own copy from the same source) |
| `pageContext.ts` | `gen-agent-types.mjs` | `src/crm/toolspec/page_context.py` — the view descriptor, which cannot carry rows |

The directory is gitignored (`frontend/.gitignore`) and never hand-edited. Import from
`src/types/index.ts`, not from a generated module.

Response shapes (`Account`, `Opportunity`, …) are hand-maintained in `src/types/domain.ts`.
`scripts/export_response_shapes.py` runs each covered handler against a `moto`-mocked table and emits
the real top-level key set; `src/test/responseShapes.test.ts` asserts each response carries the keys
the frontend reads.

### Design tokens (`src/styles/`)

`src/styles/tokens.css` is the single source of colour, type, spacing, radius, elevation, and motion.
Components reference tokens only — no raw hex colour, `px` font size, or off-scale spacing;
`scripts/check_design_tokens.py` enforces this. `tailwind.config.ts` maps every theme key onto
`var(--token)` via `src/styles/theme.ts`; Tailwind 4 loads it through `@config` in
`src/styles/tailwind.css`.

Token names are semantic (`--surface-raised`, `--text-muted`, `--border-subtle`). Two naming traps:

- `--text-primary|secondary|muted|inverse|on-accent|link` are colours; `--text-3xs…--text-3xl` are
  font sizes.
- `px` appears only in `--radius-pill: 9999px` and in the `--elevation-*` shadow values; sizes and
  spacing are `rem` or unitless.

| Concern | Tokens |
|---|---|
| Surfaces | `--surface-{sunken,base,raised,overlay,inset,hover,selected,inverse}` |
| Text / border / accent | `--text-*`, `--border-{subtle,default,control,strong,inverse}`, `--accent-*`, `--focus-ring-*` |
| Semantic states | `--{denied,danger,caution,positive,info}-{emphasis,surface,border,text}`, `--{empty,pending}-{surface,border,text}` |
| Approvals diff | `--diff-{removed,added}-{surface,border,text,decoration,marker}`, `--diff-unchanged-{surface,text,marker}` |
| Model-family series | `--series-{1,2}{,-surface,-fill,-dash}` |
| Type | `--font-{sans,numeric}`, `--text-3xs…3xl`, `--leading-*`, `--weight-*`, `--tracking-*`, `--measure-*` |
| Spacing / density | `--space-0…13`, `--row-height-*`, `--control-height-*`, `--cell-padding-{x,y}` |
| Radius / layout | `--radius-*`, `--layout-*`, `--breakpoint-{md,lg,xl}`, `--z-*` |
| Elevation / motion | `--elevation-{flush,raised,overlay,popover}`, `--duration-*`, `--ease-*` |

**Themes.** Light is the `:root` default; dark is `[data-theme="dark"]` on the document element, set
before first paint from the persisted per-user preference. There is no `prefers-color-scheme` block,
so the theme changes only when that attribute is written.

**Non-colour cues.** Pending proposals also carry `--pending-border-style: dashed` and
`--pending-hatch`; diff sides carry `--diff-*-decoration` and a `--diff-*-marker` glyph; the two model
families carry `--series-{1,2}-fill` and `-dash`. Apply the pattern as well as the hue.

IBM Plex is self-hosted via `@fontsource/ibm-plex-{sans,mono}`, imported from `src/styles/fonts.css`,
so the app makes no third-party font request. Keep those imports out of `tailwind.css`: Tailwind
inlines every `@import` in its entry, and the faces' relative `url()`s then fail to resolve.

`src/index.css` holds only the global `:focus-visible` ring and the `.visually-hidden` utility, both in
`@layer base` so utilities override them; everything else comes from `tokens.css` and Tailwind
(`.sr-only` is Tailwind's).

### UI primitives (`src/components/ui/`)

shadcn/ui + Radix primitives bound to the token vocabulary: `button`, `input`, `select`, `combobox`
(with `command` and `popover`), `dialog`, `sheet`, `tooltip`, `table`, `tabs`, `toast`, `badge`,
`skeleton`. Use these rather than hand-rolling an interactive primitive. Every interactive primitive
spreads `focusRing` from `utils.ts`. Tailwind 4 is wired through `postcss.config.js`
(`@tailwindcss/postcss`) → `src/styles/tailwind.css`, which declares the content sources (`@source`)
and loads `tailwind.config.ts` (`@config`). `src/main.tsx` imports `index.css`, `styles/fonts.css`
and `styles/tailwind.css` once each.

`tailwind.css` also restores Tailwind 3 output for the defaults and utilities Tailwind 4 changed: the
default border colour, placeholder colour, field background, option padding, search-field appearance
and button cursor (base layer); `outline-none`, `space-y-*`, `divide-y`, `divide-border-*` and
`max-w-prose` (utilities layer); and the type scale's inherited line height (`@theme`). Pages render
identically at rest. `src/styles/compat.test.ts` compiles the entry and fails if any restored rule is
removed or loses to Tailwind's own; `src/index.css.test.ts` fails if a rule in `index.css` leaves
`@layer base`. A `space-y-*`, `divide-y` or `divide-border-*` class new to the source needs a rule
there; `src/styles/tailwind.test.ts` fails until it has one.

**Tailwind 4 differences that remain** (measured against the Tailwind 3 build; not restored):

- **Dialog open animation.** A centred dialog (`-translate-x-1/2 -translate-y-1/2` with
  `animate-in zoom-in-95`) now zooms in place. Tailwind 4 centres with the `translate` property, which
  composes with the `transform` the enter keyframe animates, so the dialog no longer slides in from
  off-centre. The final position is unchanged, and with reduced motion there is no animation.
- **Transitions include the outline.** `transition` and `transition-colors` also transition
  `outline-color` (and `translate`, `scale`, `rotate`), so a focus outline fades in over the
  transition duration.
- **Hover only on hover-capable devices.** `hover:` rules are emitted inside `@media (hover: hover)`.
  A touch tap no longer leaves a hover style behind.
- **`[hidden]` always wins.** Preflight's `[hidden]:where(:not([hidden="until-found"]))` rule is
  `display: none !important`. The hidden attribute now beats any display utility, so the panel's
  `[&[hidden]]:hidden` guard is redundant (and harmless).
- **Browser floor.** Tailwind 4 relies on cascade layers, `@property` and `color-mix()`, so it needs
  Safari 16.4+, Chrome 111+ or Firefox 128+ (Tailwind's stated targets). There is no autoprefixer, so
  older prefixed forms such as `-moz-user-select` are no longer emitted. Vite 8's default
  `build.target` (`baseline-widely-available`: Chrome 111, Safari 16.4, Firefox 114) matches on
  Chrome and Safari but is lower on Firefox, so Tailwind sets the floor.

**Adding a token scale key touches three files:** `tokens.css`, `theme.ts`, and
`CUSTOM_CLASS_GROUPS` in `src/components/ui/utils.ts`. Without the third, `cn()` (`tailwind-merge`)
silently drops the utility at runtime. `utils.test.ts` asserts the files agree.

Gotchas that pass lint, typecheck and the token gate and show only in a browser:

1. **Keep Tailwind's preflight enabled.** It supplies `border-style: solid`; without it every
   `border-width` computes to `0`, and `<button>` keeps its UA appearance.
2. **`cn()` must know every custom scale key** (above); an undeclared key is misread as a colour or
   fails to override.
3. **`border-hairline` and `border-emphasis` set all four sides.** For one edge use
   `border-b-hairline`, not `border-b border-hairline`.

### Assistant panel (`src/components/AssistantPanel/`)

One region, present on every signed-in route, that keeps its conversation across navigation.

```
AppShell                       renders the panel as a sibling of the router <Outlet />
  AssistantPanelProvider       (PanelProvider.tsx) holds the conversation across turns
    <Outlet />  <-- pages
    Panel                      visibility, focus, Escape, Cmd/Ctrl+Shift+A
      AssistantConversation    composer, transcript, metering, proposal actions
      PanelTuning              per-session model tuning
```

`Panel` renders `children` and does no network I/O. `AssistantConversation` is the only production
caller of `useAgentStream` (the `/agent/*` SSE stream), `useCancelTurn` (cooperative cancel) and
`useAssistantTurn` (the scope disclosure). `usePageContext` has two callers: `AssistantConversation`
passes the view's report to build the page-context descriptor sent with a turn, and `Panel` calls it
without a report for the empty-state suggestions.
`AppShell` is the only place that renders `Panel`, so anything the panel shows is passed there.

`useAgentStream` holds the current turn and clears it on each `sendPrompt`; the conversation across
turns lives in `AssistantPanelProvider`, above the `<Outlet />`, so it survives navigation and closing.

### `/agent/*` requests (`src/lib/agentClient.ts`)

The relay ([`docs/relay-artefact.md`](../docs/relay-artefact.md)) sits behind CloudFront Origin
Access Control. `buildRelayRequest` is the one place the
headers are composed; `useAgentStream` and `useCancelTurn` both use it.

| Header | Value | If wrong |
|---|---|---|
| `X-Crm-Authorization` | `Bearer <Cognito access token>` | OAC overwrites `Authorization` with CloudFront's SigV4, so a token there never reaches the relay (401). The relay reads only this header (`src/relay/src/jwt.ts`). |
| `x-amz-content-sha256` | lowercase hex SHA-256 of the exact body bytes | A Lambda Function URL origin under OAC rejects an unsigned or mismatched payload with a CloudFront 403. |

```ts
const request = await buildRelayRequest({ token, accept: "text/event-stream", body });
await fetch(url, { method: "POST", headers: request.headers, body: request.body });
```

Send `request.body` exactly as returned — re-serializing the object can change the bytes the digest
covers. Omitting `body` sends the empty-payload hash. `src/lib/agentClient.test.ts` pins the digests,
and `src/hooks/useAgentStream.test.ts` reads `src/relay/src/jwt.ts` so a header rename on either side
fails in `vitest`.

### Stream reconnection

`useAgentStream` reconnects by session id only when a read **throws** on a non-aborted stream. It does
not reconnect on:

| Event | Behaviour |
|---|---|
| 401 / 403 | Terminal, with `failureReason` `unauthenticated` (sign in again) or `forbidden` (not a sign-in problem) |
| An abort issued by the hook (unmount, or a new `sendPrompt`) | Decided by `controller.signal.aborted`; the aborted stream writes no state |
| A tear after the user pressed Stop | `stream.markTurnCancelled()` is called when the cancel is requested; cleared by the next `sendPrompt` |
| A clean end-of-body with no terminal event | Treated as finished |

A reconnect waiting out its backoff is also guarded by `mountedRef`. An unknown event `kind` renders
as an `unrecognised` entry, and an unparseable frame renders as a `MALFORMED_FRAME_KIND` event with a
bounded excerpt — no frame is dropped silently.

### Page context (`useReportView` → `viewReport.ts`)

A page describes its view to the panel through a store:

```ts
useReportView({ query: { filters: appliedFilters }, rowCount: rows.length, truncated });
```

- Report the filters that produced the rows on screen, not unsubmitted form state.
- Pass `truncated` whenever the view has a ceiling, so the count is treated as a lower bound.
- A snapshot is stamped with its route and ignored on any other route.
- Only counts and scalar filters are carried — never rows.
- A page that cannot describe its view passes `inexpressible`, rendered as "this view can't be
  summarised".

### Wire contracts

- **`sendPrompt(prompt, { pageContext, tuning })`** — the second argument is a wrapper whose fields are
  all optional, so passing a bare tuning object type-checks but sends no tuning.
  `useAgentStream.tuning.test.ts` pins this, including replay on reconnect.
- **`toWireTuning` (`wireTuning.ts`)** — the browser holds one `{family, model, effort}` selection
  per tier (`sessionTuning.ts`), loaded from and saved to `POST /api/user_settings`. Turns and saves
  carry the schema-3 block `{schema: 3, source: "settings.tuning", tiers: {supervisor, subagent,
  tool}}`, every tier with exactly those three fields (`null` = that tier's default), decoded
  strictly by `providers.tier_tuning.parse_wire_tuning`. A turn carries no block when nothing is
  selected. `wireTuning.test.ts` writes `tests/integration/fixtures/wire_tuning.json` for the Python
  side.
- **Server rejections at the control** — a `validation_failed` save naming
  `tuning.tiers.<tier>.<field>` is shown at that tier's family, model or effort control
  (`controlForServerSource` in `validateTuning.ts`). A field with no control here, such as a removed
  parameter sent by a page loaded before a deploy, is shown as an alert at the top of that tier's
  fieldset rather than as a failed save.

### Panel accessibility

Exactly one `aria-live="polite"` region over the transcript, always mounted (in
`AssistantConversation`). `PanelTuning` has its own live region for its loading state, so scope any
live-region count to the transcript. Closing the panel returns focus to the element that had it, or to
the toggle (resolved through the toggle's `aria-controls`) when focus was on `<body>`.

Known gaps:

- `useAssistantTurn.buildRequest` has no production caller; `sendPrompt` builds its own body, and
  `useAgentStream.pageContext.test.ts` asserts the two agree key-for-key.
- The app has no logging layer; a failed proposal decision is surfaced in the DOM only.
- `src/test/a11y.test.tsx` does not scan `AssistantPanel/**`.

```bash
npm run test -- AssistantPanel        # the panel's own suite
npm run test -- useAgentStream        # transport, reconnect, tuning, descriptor replay
```

### Accessibility

`src/test/a11y.test.tsx` runs `axe-core` over every page under `src/pages/` and the signed-out
sign-in surface (`src/auth/SignInForm.tsx`) with realistic mocked content, failing on any `serious` or
`critical` violation. `color-contrast` is disabled there (jsdom has no paint engine) and checked at the
token level instead:

- `src/index.css.test.ts` computes the WCAG AA ratio of `--text-primary` on `--surface-base`, in both
  themes.
- `src/styles/tokens.test.ts` checks every semantic text pair at 4.5:1 on each surface it can sit on,
  `--border-control`, `--border-strong`, `--focus-ring-color` and both series marks at 3:1, that the
  Tailwind theme references only defined tokens, that the state surfaces are pairwise distinct, and
  that durations collapse to `1ms` under `prefers-reduced-motion`.

```bash
npm run test -- a11y
npm run test -- css.test
npm run test -- tokens
```

## See also

- [Repository documentation map](../README.md#documentation)
- [Eval dashboard page](src/pages/evals/README.md) and
  [`docs/eval-methodology.md`](../docs/eval-methodology.md), for what the `/evals` page renders
- [T4 browser journeys](../tests/e2e/browser/journeys/README.md), which drive this SPA against a
  deployed stack
- [`docs/relay-artefact.md`](../docs/relay-artefact.md), for the relay behind `/agent/*`

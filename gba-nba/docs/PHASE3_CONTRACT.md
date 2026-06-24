# Phase 3 wire contract — Sales Cockpit (authoritative, do not deviate)

Three layers must agree exactly:

    gba_console (React/TS)  ──HTTP──►  gba-server (.NET proxy)  ──HTTP──►  gba-nba (FastAPI)
    PascalCase bodies                  injects manager NetId          snake_case bodies
                                       from the SESSION (never client)  resolves NetId→int

Identity rule (security-critical): the console NEVER sends a manager id. gba-server reads the
authenticated user's `NetId` (GUID) from `User.TryGetNetId(out var netId)` and forwards it.
gba-nba resolves that GUID to the integer `dbo.User.ID` and verifies task ownership.

---

## Layer 3 — gba-nba (FastAPI) new endpoints

DB resolution (add to `app/data/signals_repository.py`):

    def manager_id_for_netuid(net_uid: str) -> int | None:
        # dbo.User has columns: ID (bigint), NetUID (uniqueidentifier), Deleted (bit)
        rows = query("SELECT ID AS id FROM dbo.[User] WHERE NetUID = :nu AND Deleted = 0", {"nu": net_uid})
        return int(rows[0]["id"]) if rows else None

Lifecycle helpers (add to `app/services/lifecycle.py` if missing):
- `get_task(task_key) -> dict | None` — raw mongo doc (for ownership check).
- `count_active_by_urgency(manager_id) -> dict` — {"critical":n,"high":n,"normal":n,"low":n,"total":n}
  counting tasks in ACTIVE statuses (open/in_progress + snoozed past due treated as active),
  consistent with how `inbox()` decides what is surfaced.

New API endpoints in `app/api/main.py` (a `/cockpit/*` group; KEEP existing endpoints intact).
All resolve `manager_net_uid` → int via `signals_repository.manager_id_for_netuid`; on unknown GUID
return HTTP 404 `{"detail":"unknown_manager"}`. On ownership mismatch return HTTP 403.

1. `GET /cockpit/inbox?manager_net_uid={guid}&limit=50&status=open,in_progress,snoozed`
   - `status` is an optional CSV; if omitted use the inbox default.
   - returns: `{"manager_id":int,"manager_net_uid":str,"count":int,"tasks":[<task docs, _id stringified>]}`

2. `GET /cockpit/count?manager_net_uid={guid}`
   - returns: `{"manager_id":int,"active_count":int,"by_urgency":{"critical":int,"high":int,"normal":int,"low":int}}`

3. `POST /cockpit/status?manager_net_uid={guid}`
   body: `{"task_key":str,"to":str,"reason":str|null,"sold":bool|null,"amount":float|null,"snooze_until":datetime|null}`
   - resolve net_uid→int (this is the `by` actor); load task; if task.manager_id != resolved id → 403.
   - build Outcome only when to==done and (sold|amount) given; call `lifecycle.change_status`.
   - returns the updated task doc (_id stringified). On illegal transition → 400 (TransitionError).

4. `POST /cockpit/notes?manager_net_uid={guid}`
   body: `{"task_key":str,"text":str}`
   - resolve→int (this is author_id); ownership check (403); `lifecycle.add_note`.
   - returns updated task doc. Missing task → 404.

5. `POST /cockpit/generate?manager_net_uid={guid}&as_of_date={YYYY-MM-DD|null}`
   - resolve→int; `orchestrator.generate_for_manager`. returns the stats dict.

Pydantic request models (snake_case fields exactly as above). Reuse existing `TaskStatus`, `Outcome`.

Tests: add `tests/test_cockpit_api.py` using FastAPI `TestClient` + mongomock + a monkeypatched
`manager_id_for_netuid` (so no live DB needed). Cover: inbox returns only that manager's tasks;
count shape; status happy path; ownership 403; unknown-manager 404; illegal transition 400.

---

## Layer 2 — gba-server (.NET) proxy. MIRROR the existing reco integration EXACTLY.

Reference files to copy the pattern from (read them first):
- Controller: `src/Global.Business.Assistant.Api/Controllers/RecommendationsController.cs`
- Service:    `src/Global.Business.Assistant.Application.Services/Services/Recommendations/ProductRecommendationService.cs`
- Contracts:  `src/Global.Business.Assistant.Application.Contracts/Services/Recommendations/...`
- DI:         `src/Global.Business.Assistant.Application.Services/DependencyInjection/ConcordServicesServiceCollectionExtensions.cs`
- Config:     `ConfigurationManager` (SharedKernel.Helpers) + `appsettings.json`
- User id:    `src/Global.Business.Assistant.WebApi/Extensions/ClaimsPrincipalExtensions.cs` → `User.TryGetNetId(out Guid netId)`
- Routing:    `src/Global.Business.Assistant.WebApi/WebApi/RoutingConfiguration/Maps/ApplicationSegments.cs` and `RecommendationsSegments.cs`

DO NOT TOUCH any DataSync files (WIP): the `*Operations*DataSync*` projects and `Platform.Actors/Actors/DataSync/*`.

### Routing (pinned strings — console depends on these EXACT paths)
Add to `ApplicationSegments.cs`:  `public const string SalesCockpit = "sales/cockpit";`
New `Maps/SalesCockpitSegments.cs`:
    INBOX    = "inbox"
    COUNT    = "count"
    STATUS   = "tasks/status"
    NOTES    = "tasks/notes"
    GENERATE = "generate"
→ full paths: `/sales/cockpit/inbox`, `/sales/cockpit/count`, `/sales/cockpit/tasks/status`,
  `/sales/cockpit/tasks/notes`, `/sales/cockpit/generate`.

### Config
appsettings.json (+ appsettings.Development.json if present): add section
    "GbaNbaApi": { "Url": "http://127.0.0.1:8002", "TimeoutSeconds": 30 }
ConfigurationManager: add `GbaNbaApiUrl` (string) and `GbaNbaApiTimeoutSeconds` (int), loaded the
same way as `RecommendationApiUrl`/`RecommendationApiTimeoutSeconds` (find and mirror that code).

### Contracts project — `Application.Contracts/Services/SalesCockpit/`
`Contracts/ISalesCockpitService.cs` (returns `System.Text.Json.JsonElement` passthrough — do NOT mirror the whole Task schema):
    Task<JsonElement> GetInboxAsync(Guid managerNetId, int limit, string statusCsv, CancellationToken ct = default);
    Task<JsonElement> GetCountAsync(Guid managerNetId, CancellationToken ct = default);
    Task<JsonElement> SetStatusAsync(Guid managerNetId, string taskKey, string to, string reason, bool? sold, decimal? amount, DateTime? snoozeUntil, CancellationToken ct = default);
    Task<JsonElement> AddNoteAsync(Guid managerNetId, string taskKey, string text, CancellationToken ct = default);
    Task<JsonElement> GenerateAsync(Guid managerNetId, string asOfDate, CancellationToken ct = default);

`Models/` — request DTOs that bind from the CONSOLE (PascalCase, default casing):
    SalesCockpitStatusRequestDto { string To; string Reason; bool? Sold; decimal? Amount; DateTime? SnoozeUntil; }
    SalesCockpitNoteRequestDto   { string Text; }
(Internal nba-bound payloads use snake_case via [JsonPropertyName] — build them inside the service,
mirroring how ProductRecommendationService DTOs carry [JsonPropertyName].)

### Service — `Application.Services/Services/SalesCockpit/SalesCockpitService.cs`
- `private const string HttpClientName = "GbaNbaApi";`
- `CreateClient()` identical shape to ProductRecommendationService but using `ConfigurationManager.GbaNbaApiUrl` / `GbaNbaApiTimeoutSeconds`.
- Each method appends `manager_net_uid={managerNetId}` (and other query params) and calls the
  matching gba-nba `/cockpit/*` endpoint; deserialize response to `JsonElement` and return.
- status/notes: POST to `/cockpit/status` / `/cockpit/notes` with a snake_case body
  `{ task_key, to, reason, sold, amount, snooze_until }` / `{ task_key, text }`.
- Register in DI: `services.AddScoped<ISalesCockpitService, SalesCockpitService>();` in `AddConcordServices`.

### Controller — `Api/Controllers/SalesCockpitController.cs`
`[Authorize]`, `[AssignControllerRoute(WebApiEnvironmnet.Current, WebApiVersion.ApiVersion1, ApplicationSegments.SalesCockpit)]`,
base `WebApiControllerBase`, ctor takes `ISalesCockpitService` + `IResponseFactory`.
Every action: `if (!User.TryGetNetId(out var netId)) return Unauthorized();` then try/catch like the
reco controller (`Ok(SuccessResponseBody(result))` / `LogError` + `BadRequest(ErrorResponseBody(...))`).
- `[HttpGet][AssignActionRoute(SalesCockpitSegments.INBOX)] GetInbox([FromQuery] int limit = 50, [FromQuery] string status = null)`
- `[HttpGet][AssignActionRoute(SalesCockpitSegments.COUNT)] GetCount()`
- `[HttpPost][AssignActionRoute(SalesCockpitSegments.STATUS)] SetStatus([FromQuery] string taskKey, [FromBody] SalesCockpitStatusRequestDto body)`
- `[HttpPost][AssignActionRoute(SalesCockpitSegments.NOTES)] AddNote([FromQuery] string taskKey, [FromBody] SalesCockpitNoteRequestDto body)`
- `[HttpPost][AssignActionRoute(SalesCockpitSegments.GENERATE)] Generate([FromQuery] string asOfDate = null)`

Build note: the full solution has ~22 PRE-EXISTING DataSync build errors that are NOT ours. Build the
Contracts and Services projects individually to verify our code is clean; for the Api project, confirm
no NEW errors beyond those known DataSync ones.

---

## Layer 1 — gba_console (React/TS). New feature `src/features/sales-cockpit/`.

Console→server contract (PascalCase bodies, leading-slash paths, no manager id ever):
- `GET  /sales/cockpit/inbox`         query `{ limit?, status? }`            → inbox payload
- `GET  /sales/cockpit/count`                                              → count payload
- `POST /sales/cockpit/tasks/status`  query `{ taskKey }`  body `{ To, Reason?, Sold?, Amount?, SnoozeUntil? }`
- `POST /sales/cockpit/tasks/notes`   query `{ taskKey }`  body `{ Text }`
- `POST /sales/cockpit/generate`      query `{ asOfDate? }`

Files:
- `types.ts` — `CockpitTask`, `CockpitTaskStatus`, `CockpitInbox`, `CockpitCount`, `Explanation`,
  `Contact`, `Note`, urgency/type unions. Mirror the gba-nba Task doc fields:
  task_key, manager_id, client_id, client_name, task_type, title, reason, priority, urgency, status,
  payload, signals, explanation{factors,source_signal,confidence}, contact{phone,email,viber,preferred},
  due_date, sla_breached, notes[], snooze_until, ab_variant, generated_at, updated_at. (snake_case keys
  as returned by gba-nba; type them as-is.)
- `api/salesCockpitApi.ts` (+ `.test.ts`) — wrap `apiRequest` per the shared pattern; one function per
  endpoint: `getCockpitInbox`, `getCockpitCount`, `setTaskStatus`, `addTaskNote`, `regenerateCockpit`.
  Normalize array/shape defensively (like accountingCashFlowApi). Test mocks `apiClient` (vi.mock) and
  asserts each call's path/method/query/body EXACTLY (mirror existing *Api.test.ts).
- `components/` — `TaskCard.tsx` (priority/urgency badge, title, reason, client, one-click contact via
  tel:/mailto:, action buttons), `WhyThisTask.tsx` (collapsible showing explanation.factors + signals +
  confidence), `TaskFilters.tsx` (type + urgency filters), `NoteModal.tsx` + `SnoozeModal.tsx`
  (AppModal-based). Use Mantine Card/Group/Stack/Badge/Button/ActionIcon. All text via `t('…')` (Ukrainian).
- `pages/SalesCockpitPage.tsx` — load inbox via useEffect + useValueState + AbortController; priority-sorted
  cards; filters; Done/Snooze/Dismiss/Note actions calling the api then refreshing; loading/empty/error states.
- `index.ts` — re-export `SalesCockpitPage` + public types.

Wiring:
- `src/app/routes/lazyConsolePages.tsx` — add `export const SalesCockpitPage = lazy(() => import('../../features/sales-cockpit').then(m => ({ default: m.SalesCockpitPage })))`.
- `src/app/routes/consoleRoutes.tsx` — add `{ path: '/sales/cockpit', element: lazyRoute(<SalesCockpitPage />) }` to the migrated-routes array (match the existing entry style/imports).
- `src/app/layout/components/ConsoleHeader.tsx` — bell (≈L88): add a badge with the active count
  (fetch `getCockpitCount` on mount + light polling, e.g. every 60s, with cleanup) and an onClick that
  navigates to `/sales/cockpit`. Keep the existing markup/classes; only add badge value + handler.
  Note: the left-nav sidebar menu is backend-driven (`/dashboards/modules/all/role`) so a formal menu
  item is out of console scope — the bell + direct route make the cockpit reachable.

Verify: `npx tsc --noEmit` clean for the new files; `npx vitest run` for the new api test green;
match repo lint/format conventions (no new eslint errors).

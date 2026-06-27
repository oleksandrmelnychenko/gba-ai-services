export const meta = {
  name: 'gba-forecast-build',
  description: 'Build the gba-forecast Python service (:8006) + the gba-server SalePrediction proxy — disjoint repos, in parallel',
  phases: [
    { title: 'Build', detail: 'PY: gba-forecast service (forecast engine, reuses procure) ; NET: SalePrediction proxy (clones ProductIntelligence)' },
  ],
}

const CONTRACT = `
WHY: the console «Прогноз продажів» (src/features/sales-prediction) calls GET /sales/prediction/get?clientNetId=&productNetId=
and reads result.ByClient / result.ByProduct / result.ByClientAndProduct — each an array of points
{ SaleAmount: number, MonthNameUK: string }. The old ML backend was removed in the net10 re-arch (404 now → "на 0 місяців").
We re-implement it as a new Python forecast service + restore the gba-server proxy at the SAME route, so the FRONTEND IS
UNTOUCHED.

RESPONSE CONTRACT (exact, PascalCase, so it flows unchanged through the proxy to the console reader):
  {
    "ByClient":           [ { "SaleAmount": <number EUR>, "MonthNameUK": "<укр місяць рік, e.g. 'Лип 2026'>" }, ... ],
    "ByProduct":          [ ... same shape ... ],
    "ByClientAndProduct": [ ... same shape ... ]
  }
Behavior: client-only request -> ByClient populated (ByProduct/ByClientAndProduct may be []); product-only -> ByProduct;
both -> all three (ByClientAndProduct = forecast for that client buying that product). Forecast horizon = next N months
(default 6, config). MonthNameUK = Ukrainian short month + year. Amounts in EUR.
`

const REPORT = {
  type: 'object', additionalProperties: false,
  required: ['component', 'files', 'how_to_run_or_wire', 'live_or_build_evidence', 'notes'],
  properties: {
    component: { type: 'string' },
    files: { type: 'array', items: { type: 'string' } },
    how_to_run_or_wire: { type: 'string', description: 'how the lead runs/deploys it + the exact endpoint/route + any config knobs' },
    live_or_build_evidence: { type: 'string', description: 'real proof: a sample response on real data (PY) or what compiles + route (NET)' },
    notes: { type: 'string' },
  },
}

phase('Build')
const out = await parallel([
  () => agent(`${CONTRACT}

YOU ARE PY — build the new Python service **gba-forecast** at /root/projects/gba-forecast (NEW repo dir), port **8006**.
MIRROR the gba-products service layout EXACTLY (read /root/projects/gba-products: app/core/{config,logging,metrics}.py,
app/data/{db,cache}.py, app/api/main.py, pyproject.toml, .env.example, the internal-key middleware, tests, .gitignore).
Run/test with the shared venv: /root/projects/gba-procure/.venv/bin/{python,ruff} from the repo dir. Read-only over
ConcordDb_V5; copy the DB creds into a local .env (grep DB_* from /root/projects/gba-products/.env). Run OPEN (no key) like
gba-products for now (INTERNAL_API_KEY empty).

FORECAST ENGINE — REUSE procure's forecasting, do NOT reinvent: read /root/projects/gba-procure/app/services/forecasting/
demand.py and reuse its moving_avg / croston / sba logic (import or copy the pure functions) to project monthly sales.

SIGNALS (app/data/signals_repository.py) — read-only SQL (parameterized, EUR rules: OrderItem.PricePerItem is ALREADY EUR;
window on Order.Created NOT OrderItem.Created; o.Deleted=0 AND oi.Deleted=0). Build monthly SALE amount (EUR =
SUM(oi.Qty*oi.PricePerItem)) grouped by FORMAT(o.Created,'yyyy-MM') for: (a) a client (via Client.NetUID -> ClientAgreement
-> Order/OrderItem), (b) a product (via OrderItem.ProductID where Product.NetUID), (c) client AND product. Resolve
clientNetId/productNetId (uuid) to the rows. Use a trailing history window (e.g. 24 months) as the model input.

ENDPOINT: GET /forecast/sales?client_net_id=&product_net_id=&months=  -> returns the EXACT response contract above
(ByClient/ByProduct/ByClientAndProduct of {SaleAmount, MonthNameUK}). Compute only the series whose id is provided; forecast
the next N months from the monthly history via the procure method; MonthNameUK = Ukrainian short month + year (map month->
['Січ','Лют','Бер','Кві','Тра','Чер','Лип','Сер','Вер','Жов','Лис','Гру']). Also /health + /metrics like gba-products.
If a series has too little history, return [] for that key (the console shows "немає даних") — never crash.

VERIFY LIVE: find client "АВАНТАЖ" (search dbo.Client.FullName LIKE '%АВАНТАЖ%' for its NetUID), call your endpoint for that
client, and SHOW the real ByClient array (months + amounts). Confirm it's non-empty + plausible EUR. ruff + pytest (a pure
unit test of the month-name mapping + the forecast-from-series shaping; a DB-integration smoke marked+skippable).

Deliver via schema: files created, how the lead runs it (uvicorn cmd + port + endpoint), the live АВАНТАЖ sample, notes.
Do NOT git-init, do NOT touch other repos.`,
    { label: 'PY-gba-forecast', phase: 'Build', schema: REPORT, effort: 'high' }),

  () => agent(`${CONTRACT}

YOU ARE NET — restore the gba-server proxy so GET /sales/prediction/get reaches the new gba-forecast service.
Repo: /root/projects/gba-server (branch development). MIRROR the EXISTING ProductIntelligence proxy EXACTLY (it is the
canonical read-only GET proxy):
  - Controller: src/Global.Business.Assistant.Api/Controllers/ProductIntelligenceController.cs
  - Service:    src/Global.Business.Assistant.Application.Services/Services/ProductIntelligence/ProductIntelligenceService.cs
  - Interface:  src/Global.Business.Assistant.Application.Contracts/Services/ProductIntelligence/Contracts/IProductIntelligenceService.cs
  - AiHttpClient: src/Global.Business.Assistant.Application.Services/Services/Ai/AiHttpClient.cs (reuse Configure + GetJsonAsync<JsonElement>)
  - Config: src/Global.Business.Assistant.SharedKernel/Helpers/ConfigurationManager.cs (the ProductsApiUrl/Key/Timeout props) +
            ConfigurationStringNames.cs ; appsettings.json + appsettings.Development.json (the "ProductsApi" section shape)
  - Routing: WebApi/RoutingConfiguration/Maps/ProductIntelligenceSegments.cs + ApplicationSegments.cs
  - DI: Application.Services/DependencyInjection/ConcordServicesServiceCollectionExtensions.cs (the AddScoped line)

BUILD a SalePrediction proxy:
  - Reuse the EXISTING segment ApplicationSegments.SalePrediction (= "sales/prediction") — ALREADY defined in GBA.Common.
  - Controller SalePredictionController : WebApiControllerBase, [Authorize], [AssignControllerRoute(..., ApplicationSegments.SalePrediction)],
    ONE action [HttpGet][AssignActionRoute("get")] GetAsync([FromQuery] Guid clientNetId, [FromQuery] Guid productNetId) ->
    so the full route is sales/prediction/get (EXACTLY what the console calls). Pass-through raw JsonElement (no DTO), same
    try/catch -> AiServiceException -> StatusCode mapping as ProductIntelligenceController.
  - Service ISalePredictionService + SalePredictionService: HttpClientName/ServiceName = "ForecastApi"; CreateClient() via
    AiHttpClient.Configure(client, ServiceName, "ForecastApi", ConfigurationManager.ForecastApiUrl, ForecastApiKey,
    ForecastApiTimeoutSeconds, 30); call GetJsonAsync<JsonElement> on the gba-forecast path
    "forecast/sales?client_net_id={clientNetId}&product_net_id={productNetId}" (omit empty Guid params — Guid.Empty => omit).
  - Add ConfigurationManager "ForecastApi" props (Url/TimeoutSeconds/ApiKey) mirroring ProductsApi; add the "ForecastApi"
    section to appsettings.json (Url:"") + appsettings.Development.json (Url:"http://127.0.0.1:8006", TimeoutSeconds:30, ApiKey:"").
  - DI: one AddScoped<ISalePredictionService, SalePredictionService>() line.
  - A ForecastSegments map if the pattern needs it (or inline the "get" action const where ProductIntelligence puts its action consts).

Validate: run 'dotnet build src/Global.Business.Assistant.Api/Global.Business.Assistant.Api.csproj' (report compile result). Do NOT
commit, deploy, or touch infra/console. Deliver via schema: files created/edited, the resulting public route, the appsettings
+ ConfigurationManager additions, the dotnet build result, and the ForecastApi__Url env the lead must add to compose.`,
    { label: 'NET-saleprediction-proxy', phase: 'Build', schema: REPORT, effort: 'high' }),
])

const ok = out.filter(Boolean)
log(`Build: ${ok.length}/2 components built`)
return { components: ok }

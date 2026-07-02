# AI Fleet — план виправлень і тюнінгу

Джерело: E2E-аудит 2026-07-02 (63 роути, 7 сервісів, реальні дані ConcordDb_V5/Mongo; 32 підтверджені дефекти: 1 high / 15 medium / 16 low; 38/38 ground-truth звірок зійшлися).

Наскрізні правила виконання: все тільки на dev; рестарти сервісів тільки через systemctl; прод не чіпається; коміти → gba-ai-services `main`, інфра → gba-infra; кожен перф-фікс — з benchmark до/після і звіркою цифр з ground-truth SQL з аудиту.

## Етап 0 — Безпека (перший, ~1–2 год, без даунтайму)

1. **Host-firewall для :8000–8006.** Сервіси слухають `0.0.0.0` на публічному IP (85.17.167.167), INPUT policy ACCEPT — відкриті в інтернет, сканери активно пробують. Розширити `gba-infra/scripts/harden-docker-firewall.sh` (той самий юніт `gba-docker-firewall.service`) INPUT-правилами: allow ESTABLISHED, потім `-i <public-iface> -p tcp --dports 8000:8006 -j DROP`. gba-server це НЕ зачепить — він ходить через `host.docker.internal` (docker-bridge, інший інтерфейс). SSH не чіпаємо (правила тільки на конкретні порти).
2. **API-ключі для gba-products і gba-forecast** (зараз порожні ⇒ auth повністю вимкнено). Plumbing готовий: `AiHttpClient` шле `X-Internal-Api-Key`, у compose є порожні `ProductsApi__ApiKey`/`ForecastApi__ApiKey`. Згенерувати ключі → `.env` обох сервісів + compose env → `systemctl restart` двох юнітів + recreate `gba-dev-data-concord`. У gba-forecast прибрати/вимкнути `ALLOW_OPEN_INTERNAL_API`.
3. **Верифікація:** лічильники iptables ростуть на DROP; сканерські 401/`Invalid HTTP request` у логах припиняються; консоль dev:8083 працює e2e (форкаст на клієнтській картці, асортимент). Заодно підтвердити, що DOCKER-USER реально ріже опубліковані 27017 (mongo) / 6379 (redis) з інтернету.

## Етап 1 — Рантайм-гігієна gba-procure (~30 хв)

- `gba-procure.service` працює на venv від gba-reco (та сама помилка, що була в pricing) ⇒ `pulp` відсутній, `method=milp` мовчки рахує greedy.
- Звірити `gba-procure/.venv` має всі runtime-deps (importlib-перевірка, як робили для pricing) → поправити ExecStart → daemon-reload → restart → health.
- E2E-доказ: свіжий (некешований) `method=milp` без `milp_failed_fallback_greedy` у лозі.

## Етап 2 — Коректність API по всьому флоту (~1 день коду)

Спільний патерн (усі 7 сервісів):
- `as_of_date`: голий `str` → `datetime.date | None` (pydantic body) / валідований Query (GET) ⇒ 422 замість 500.
- Санітизація помилок: заборонити `str(exc)` у `HTTPException.detail` — зараз reco/solvency/pricing віддають клієнту повний SQL з параметрами. Повний exception → лог; клієнту generic message + error_id.

Точкові фікси:
- **gba-nba:** `/generate` не повертає «тихий 200 success» коли генератори впали (errors[]/generators_failed у відповіді, non-2xx при повному провалі); `strptime` у `/cockpit/target` → 422; `TransitionError` not-found → 404 (консистентно з рештою); `all_managers()` фільтрує `User.Deleted=0` (зараз видалені менеджери 10150/10156 у head-в'ю з задачами); полагодити подвійний лік `skipped_capped` у pass-3.
- **gba-pricing:** cache key має включати `target_margin_pct`/`with_vat`/`culture` (зараз віддає stale з чужими параметрами — найважливіший тут); `DELETE /cache` нормалізує UID у lowercase; прибрати подвійний лік у `/metrics` (middleware + service).
- **gba-reco:** `DELETE /cache/{cid}` чистить і `copurchase:*` ключі; GUID/дата-валідація у batch без SQL у `errors[]`.
- **gba-procure:** `method` → `Literal['greedy','milp']`; `budget_eur` → `Field(gt=0)`; додати `method_used` у відповідь.
- **gba-solvency:** `months` → `ge=1, le=60` (як у /score); `/charts` гейтити по `has_buyer_role` (зараз не-покупець отримує флет-100 sparkline, а /score його відсікає).
- **gba-products:** фільтри `band/abc/xyz/lifecycle` → Literal + case-normalize (зараз `band=bogus` мовчки дає порожній 200).

Верифікація: повторний прогін збережених repro-команд аудиту + unit-тести на валідацію.

## Етап 3 — Критичний reco discovery 500 (~півдня)

- `collaborative_products` (app/data/sales_repository.py): гігантський клієнтський `NOT IN` (тисячі параметрів, pymssql інтерполює на клієнті) ⇒ >25с таймаут для клієнтів з широкою історією (410207: 6119 товарів; 411706: 11555). Переписати exclusion server-side: `NOT EXISTS` до власних замовлень клієнта (0 параметрів) або OPENJSON-джойн (патерн 8b413145 з gba-server).
- Graceful degradation: якщо discovery падає/таймаутить — повертати repurchase-only з прапорцем, не 500.
- Верифікація: 410207/411706 → 200 за <5с; нічний warm run `failed: 0` (10+ днів був `failed: 2`); контрольний клієнт 410246 — ranking незмінний (18/18 у тому ж порядку).

## Етап 4 — Перфоманс-тюнінг (benchmark-driven, по одному)

| # | Ціль | Зараз | Фікс | Таргет |
|---|---|---|---|---|
| 1 | nba `/cockpit/dashboard` | 23–50с | scalar UDF `GetExchangedToEuroValue` на кожен рядок боргу (×2) → set-based конверсія: join курсів раз на валюту/дату | <3с |
| 2 | nba `/cockpit/head/dashboard` | 87–135с | серійний per-manager борговий SQL → один set-based запит по всіх менеджерах (або паралель) | <10с |
| 3 | solvency `/charts` cold | 25–32с | 12 point-in-time рескорів на запит, воркери=2 → персистентний кеш скорів закритих місяців (незмінні ⇒ без TTL), рахувати тільки поточний | <5с |
| 4 | products cold build | 12–25с | `FORMAT(Created,'yyyy-MM')` = CLR на кожен з 1.22М рядків → `YEAR()`/`MONTH()` групування + прогрів overview у шедулері | <3с |
| 5 | procure некешований cart-plan | 34–56с | кеш тільки дефолтного ключа → кешувати пер-producer плани, збирати будь-яку комбінацію limit/budget/method з них | <5с |

Кожен: замір до/після (`curl -w`) + звірка цифр відповіді з ground-truth SQL з аудиту (порядок сум/лічильників має збігтися до копійки).

## Етап 5 — Дані: SaleReturn всі Deleted=1 (розслідування, узгодити окремо)

- Всі 11 555 `SaleReturn` + 19 971 `SaleReturnItem` стоять `Deleted=1` після прогону 1C DataSync 2026-06-29 13:00 ⇒ return-фічі занулені по всьому флоту; drift-монітор solvency чесно б'є на сполох (PSI 1.07 щогодини).
- Read-only розслідування у gba-server DataSync, який саме крок масово ставить Deleted=1 (перевірити зв'язок із HIGH-знахідкою ClientReturns Dapper multi-map з попереднього аудиту DataSync). Це зона WIP користувача — тільки surface, без правок, фікс за окремим го.
- Після відновлення даних drift сам заспокоїться; за потреби — rebaseline монітора.

## Рішення за користувачем

- (a) reco: чи фільтрувати repurchase-спайн по `IsValidForCurrentSale` (зараз spine ширший за copurchase — це змінить видані рекомендації, тому без окремого підтвердження не чіпаю).
- (b) Етап 5: чи заходити в DataSync-розслідування зараз.
- (c) Порядок етапів: пропозиція 0→1→2→3→4→5.

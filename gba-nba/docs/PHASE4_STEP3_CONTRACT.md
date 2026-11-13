# Phase 4 / Step 3 — Head-of-Sales dashboard (authoritative contract)

Three layers, same pattern as the Phase 3 cockpit (see docs/PHASE3_CONTRACT.md). The head logs into
the console → sees a real-time team dashboard: every manager's target/attainment/pace + task throughput.

Identity & role: console sends NO id. gba-server forwards the session NetId. gba-nba is the AUTHORITY
on role — it resolves NetUID → UserRole.UserRoleType and returns 403 unless the caller is HEAD
(UserRoleType=6, currently uid 10162). Sales analysts are UserRoleType=0.

## Layer 3 — gba-nba (FastAPI)

signals_repository:
- `is_head_of_sales(net_uid: str) -> bool`:
    SELECT 1 FROM dbo.[User] u JOIN dbo.UserRole ur ON ur.ID=u.UserRoleID
    WHERE u.NetUID=:nu AND u.Deleted=0 AND ur.UserRoleType=6
- (reuse `all_managers()` for the team roster; reuse `targets.compute_target`.)

lifecycle (Mongo task aggregates, current month by updated_at):
- `team_stats(manager_id: int) -> dict`: {active, done_month, sold_month, dismissed_month, revenue_month}
  where active = count in ACTIVE statuses; done_month/dismissed_month = status==done/dismissed with
  updated_at in the current calendar month; sold_month = done with outcome.sold==true this month;
  revenue_month = sum(outcome.amount) for those sold-this-month.

API endpoint (role-gated):
- `GET /head/team?manager_net_uid={netId}&as_of_date=`:
    if not is_head_of_sales(netId) -> 403 {"detail":"forbidden"} (unknown netuid -> 404 via existing resolve).
    For each mid in all_managers(): row = {manager_id, target: compute_target(mid, as_of)["shipped"/"paid"
      summarized to {target,mtd,attainment_pct,pace_status}], tasks: team_stats(mid)}.
    Return {"is_head": true, "as_of": ..., "team": [rows], "totals": {shipped_target, shipped_mtd,
      paid_target, paid_mtd, done_month, sold_month, revenue_month}}.
  (Note: computing compute_target per manager is the heavy part — fine for the active roster; cache later.)

Tests (mongomock + monkeypatched is_head_of_sales + monthly_shipped/paid): head sees team; non-head -> 403;
team_stats counts done/sold/revenue for the current month only.

## Layer 2 — gba-server (.NET) — extend the existing SalesCockpit proxy

- `ISalesCockpitService.GetHeadTeamAsync(Guid managerNetId, string asOfDate, CancellationToken)` -> JsonElement.
- `SalesCockpitService`: GET `head/team?manager_net_uid={netId}` (+ as_of_date), passthrough JsonElement.
- `SalesCockpitController`: `[HttpGet][AssignActionRoute(SalesCockpitSegments.HEAD_TEAM)]` GetHeadTeam([FromQuery] string asOfDate=null):
    `if (!User.TryGetNetId(out var netId)) return Unauthorized();` then service; same try/catch as other actions.
- `SalesCockpitSegments.HEAD_TEAM = "head/team"` -> path `/sales/cockpit/head/team`.
  (Role enforcement is in gba-nba; gba-server just forwards. A 403 from gba-nba surfaces as an error.)

## Layer 1 — gba_console — head dashboard

- `api/salesCockpitApi.ts`: `getHeadTeam(asOfDate?)` -> GET `/sales/cockpit/head/team` (query {asOfDate?}).
- `types.ts`: `HeadTeamRow` {manager_id, target:{shipped:{target,mtd,attainment_pct,pace_status}, paid:{...}}, tasks:{active,done_month,sold_month,dismissed_month,revenue_month}}, `HeadTeam` {is_head, as_of, team[], totals}.
- `pages/HeadDashboardPage.tsx`: team table — columns: manager, shipped target/mtd/attainment + pace badge,
  paid attainment + pace badge, tasks done(month), sold, revenue. Sortable (leaderboard by attainment).
  Team totals header cards. Poll `getHeadTeam` every 60s (useEffect + interval cleanup, like the bell).
  Loading/empty/error + a friendly "доступ лише для керівника" on 403. All text via t('...').
- route `/sales/cockpit/head` in consoleRoutes + lazyConsolePages; reuse Mantine Card/Table/Badge/Group.
  (Real-time = 60s polling for v1; SignalR push is a later enhancement.)

Verify: gba-nba ruff+pytest (new head tests); gba-server build Contracts+Services+Api clean (ignore known
DataSync errors); console tsc --noEmit + vitest for the new api test. Do NOT touch DataSync WIP.

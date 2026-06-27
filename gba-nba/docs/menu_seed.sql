-- GBA Console nav-menu seed: add the Sales Cockpit + Head Dashboard to the DB-driven menu.
-- Idempotent (safe to re-run). Adds DashboardNode rows under the existing "Продажі"/"Sprzedaż"
-- module (uk ID=1, pl ID=2) and maps them to roles via UserRoleDashboardNode.
--   /sales/cockpit       -> "Кокпіт продажів" — visible to the same role set as /clients (everyone)
--   /sales/cockpit/head  -> "Дашборд відділу" — HeadSalesAnalytic only (UserRoleID 13)
-- Apply with a privileged login (not the read-only one), e.g. as an EF migration or:
--   docker cp menu_seed.sql gba-dev-gba-mssql-1:/tmp/ && docker exec ... sqlcmd -i /tmp/menu_seed.sql
SET NOCOUNT ON;

-- ---- DashboardNode rows (one per route per language) ---------------------------------------
MERGE dbo.DashboardNode AS tgt
USING (VALUES
    (N'Кокпіт продажів', '/sales/cockpit',      'uk', 'sales_cockpit',        1),
    (N'Kokpit sprzedaży', '/sales/cockpit',      'pl', 'sales_cockpit',        2),
    (N'Дашборд відділу',  '/sales/cockpit/head', 'uk', 'sales_head_dashboard', 1),
    (N'Panel kierownika', '/sales/cockpit/head', 'pl', 'sales_head_dashboard', 2)
) AS src(Module, Route, Language, CssClass, ModuleID)
ON tgt.Route = src.Route AND tgt.Language = src.Language AND tgt.Deleted = 0
WHEN NOT MATCHED THEN
    INSERT (NetUID, Module, Route, Language, CssClass, ParentDashboardNodeID,
            DashboardNodeModuleID, DashboardNodeType, Deleted, Created, Updated)
    VALUES (NEWID(), src.Module, src.Route, src.Language, src.CssClass, NULL,
            src.ModuleID, 0, 0, GETUTCDATE(), GETUTCDATE());

-- ---- Role mappings: cockpit -> the /clients role set ("everyone") --------------------------
INSERT INTO dbo.UserRoleDashboardNode (NetUID, UserRoleID, DashboardNodeID, Deleted, Created, Updated)
SELECT NEWID(), r.UserRoleID, dn.ID, 0, GETUTCDATE(), GETUTCDATE()
FROM (VALUES (1),(2),(3),(4),(5),(6),(7),(13),(14),(15),(20),(22),(26),(32)) AS r(UserRoleID)
CROSS JOIN dbo.DashboardNode dn
WHERE dn.Route = '/sales/cockpit' AND dn.Deleted = 0
  AND NOT EXISTS (SELECT 1 FROM dbo.UserRoleDashboardNode x
                  WHERE x.UserRoleID = r.UserRoleID AND x.DashboardNodeID = dn.ID AND x.Deleted = 0);

-- ---- Role mapping: head dashboard -> HeadSalesAnalytic only (UserRoleID 13) -----------------
INSERT INTO dbo.UserRoleDashboardNode (NetUID, UserRoleID, DashboardNodeID, Deleted, Created, Updated)
SELECT NEWID(), 13, dn.ID, 0, GETUTCDATE(), GETUTCDATE()
FROM dbo.DashboardNode dn
WHERE dn.Route = '/sales/cockpit/head' AND dn.Deleted = 0
  AND NOT EXISTS (SELECT 1 FROM dbo.UserRoleDashboardNode x
                  WHERE x.UserRoleID = 13 AND x.DashboardNodeID = dn.ID AND x.Deleted = 0);

FROM dbo.SaleReturnItem sri
JOIN dbo.SaleReturn sr ON sr.ID = sri.SaleReturnID
CROSS APPLY (SELECT CAST(sr.FromDate AT TIME ZONE 'UTC' AT TIME ZONE 'FLE Standard Time' AS datetime2) AS LocalDate) calendar
LEFT JOIN dbo.OrderItem oi ON oi.ID = sri.OrderItemID AND oi.Deleted = 0
OUTER APPLY (
    SELECT COUNT_BIG(*) AS MatchCount, MIN(candidate.ID) AS SaleID
    FROM dbo.Sale candidate
    WHERE candidate.OrderID = oi.OrderID AND candidate.ClientAgreementID = sr.ClientAgreementID
        AND candidate.Deleted = 0 AND candidate.ChangedToInvoice IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM dbo.DataSyncAccountingSourceTarget accountingTarget
            WHERE accountingTarget.TargetEntityType = N'Sale' AND accountingTarget.TargetEntityID = candidate.ID
                AND accountingTarget.IsCurrent = 1 AND accountingTarget.OwnershipType = 1)
) sourceSale
LEFT JOIN dbo.Sale s ON s.ID = sourceSale.SaleID AND sourceSale.MatchCount = 1
LEFT JOIN dbo.ClientAgreement ca ON ca.ID = sr.ClientAgreementID
LEFT JOIN dbo.Agreement ag ON ag.ID = ca.AgreementID
LEFT JOIN dbo.Product p ON p.ID = oi.ProductID
LEFT JOIN dbo.MeasureUnit mu ON mu.ID = p.MeasureUnitID AND mu.Deleted = 0
LEFT JOIN dbo.Organization org ON org.ID = ag.OrganizationID
LEFT JOIN dbo.Client cl ON cl.ID = ca.ClientID
LEFT JOIN dbo.SaleNumber saleNumber ON saleNumber.ID = s.SaleNumberID
LEFT JOIN returnCurrencyConfirmed currency ON currency.SaleReturnID = sr.ID

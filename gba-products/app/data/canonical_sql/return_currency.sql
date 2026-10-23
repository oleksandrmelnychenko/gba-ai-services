, returnCurrencyDocuments AS (
SELECT r.ID AS SaleReturnID, r.ImportedForAmg, payload.Json, rates.EurDivisor
FROM dbo.SaleReturn r
JOIN dbo.ImportedSaleReturnProjection projection ON projection.SaleReturnID = r.ID
JOIN dbo.ImportedSaleReturnRevision revision ON revision.SaleReturnID = projection.SaleReturnID
    AND revision.RevisionNumber = projection.RevisionNumber
    AND revision.SourceDocumentVersion = projection.SourceDocumentVersion
    AND revision.SourcePayloadSha256 = projection.SourcePayloadSha256
CROSS APPLY (SELECT CASE WHEN ISJSON(revision.SourcePayloadJson) = 1
    THEN revision.SourcePayloadJson END AS Json) payload
CROSS APPLY (SELECT CAST(r.FromDate AT TIME ZONE 'UTC' AT TIME ZONE 'FLE Standard Time' AS datetime2(7)) AS LocalDate) documentClock
CROSS APPLY (
    SELECT MIN(c.EurDivisor) AS EurDivisor, COUNT_BIG(*) AS Lines,
        SUM(CASE WHEN c.ProductCode IS NULL OR c.ProductCode <= 0
            OR DATALENGTH(c.SourceProductId) <> 64
            OR TRY_CONVERT(binary(16), c.SourceProductId, 2) IS NULL
            OR c.SourceCurrencyCode IS NULL OR c.SourceExchangeRate IS NULL
            OR c.EurDivisor IS NULL OR c.EurDivisor = 0 THEN 1 ELSE 0 END) AS InvalidLines,
        COUNT(DISTINCT c.EurDivisor) AS Divisors,
        COUNT(DISTINCT c.SourceCurrencyCode) AS Currencies,
        COUNT(DISTINCT c.SourceExchangeRate) AS SourceRates
    FROM OPENJSON(payload.Json, '$.CurrencyValuation.Lines') WITH (
        ProductCode nvarchar(max), SourceProductId nvarchar(max), SourceCurrencyCode nvarchar(max),
        SourceExchangeRate nvarchar(max), EurDivisor nvarchar(max)) raw
    CROSS APPLY (SELECT TRY_CONVERT(bigint,raw.ProductCode) AS ProductCode, raw.SourceProductId,
        raw.SourceCurrencyCode, TRY_CONVERT(decimal(30,14),raw.SourceExchangeRate) AS SourceExchangeRate,
        CASE WHEN raw.EurDivisor NOT LIKE '%[eE]%'
            AND (CHARINDEX('.',raw.EurDivisor) = 0
                OR SUBSTRING(raw.EurDivisor,CHARINDEX('.',raw.EurDivisor)+15,LEN(raw.EurDivisor)) NOT LIKE '%[1-9]%')
            THEN TRY_CONVERT(decimal(30,14),raw.EurDivisor) END AS EurDivisor) c
) rates
WHERE r.Deleted = 0 AND r.IsCanceled = 0 AND r.SourceDocumentID IS NOT NULL
    AND r.ImportedForAmg IS NOT NULL
    AND r.FromDate >= @FromUtc AND r.FromDate < @ToUtc
    AND JSON_VALUE(payload.Json, '$.CurrencyValuation.ContractVersion') = '2'
    AND JSON_VALUE(payload.Json, '$.FinancialSource.ContractVersion') = '1'
    AND DATALENGTH(JSON_VALUE(payload.Json, '$.SourceDocumentId')) = 64
    AND TRY_CONVERT(binary(16), JSON_VALUE(payload.Json, '$.SourceDocumentId'), 2) = r.SourceDocumentID
    AND JSON_VALUE(payload.Json, '$.ImportedForAmg') = CASE WHEN r.ImportedForAmg = 1 THEN 'true' ELSE 'false' END
    AND JSON_VALUE(payload.Json, '$.DocumentNumber') = r.Number
    AND TRY_CONVERT(bigint, JSON_VALUE(payload.Json, '$.DocumentDateTicks')) =
        DATEDIFF_BIG(DAY, CONVERT(date,'00010101'), documentClock.LocalDate) * CONVERT(bigint,864000000000)
        + DATEDIFF_BIG(NANOSECOND, CONVERT(date,documentClock.LocalDate), documentClock.LocalDate) / 100
    AND HASHBYTES('SHA2_256', CONVERT(varchar(max), payload.Json COLLATE Latin1_General_100_BIN2_UTF8))
        = revision.SourcePayloadSha256
    AND rates.Lines > 0 AND rates.InvalidLines = 0
    AND rates.Divisors = 1 AND rates.Currencies = 1 AND rates.SourceRates = 1
), returnExpectedFinancial AS (
    SELECT d.SaleReturnID, d.EurDivisor, d.ImportedForAmg, f.*
    FROM returnCurrencyDocuments d
    CROSS APPLY OPENJSON(d.Json, '$.FinancialSource.Lines') WITH (
        SourceLineNumber nvarchar(max), ProductCode nvarchar(max), SourceProductId nvarchar(max), SourceSaleId nvarchar(max),
        Quantity nvarchar(max), LineAmountEur nvarchar(max),
        SourceLineTotal nvarchar(max), SourceLineVat nvarchar(max), VatEur nvarchar(max)) raw
    CROSS APPLY (SELECT TRY_CONVERT(int,raw.SourceLineNumber) AS SourceLineNumber,
        TRY_CONVERT(bigint,raw.ProductCode) AS ProductCode, raw.SourceProductId, raw.SourceSaleId,
        raw.SourceLineTotal AS RawSourceLineTotal, raw.SourceLineVat AS RawSourceLineVat, raw.VatEur AS RawVatEur,
        CASE WHEN raw.Quantity NOT LIKE '%[eE]%'
            AND (CHARINDEX('.',raw.Quantity) = 0
                OR SUBSTRING(raw.Quantity,CHARINDEX('.',raw.Quantity)+9,LEN(raw.Quantity)) NOT LIKE '%[1-9]%')
            THEN TRY_CONVERT(decimal(28,8),raw.Quantity) END AS Quantity,
        CASE WHEN raw.LineAmountEur NOT LIKE '%[eE]%'
            AND (CHARINDEX('.',raw.LineAmountEur) = 0
                OR SUBSTRING(raw.LineAmountEur,CHARINDEX('.',raw.LineAmountEur)+15,LEN(raw.LineAmountEur)) NOT LIKE '%[1-9]%')
            THEN TRY_CONVERT(decimal(30,14),raw.LineAmountEur) END AS LineAmountEur) f
), returnExpectedCurrency AS (
    SELECT d.SaleReturnID, c.*
    FROM returnCurrencyDocuments d
    CROSS APPLY OPENJSON(d.Json, '$.CurrencyValuation.Lines') WITH (
        ProductCode nvarchar(max), SourceProductId nvarchar(max)) raw
    CROSS APPLY (SELECT TRY_CONVERT(bigint,raw.ProductCode) AS ProductCode, raw.SourceProductId) c
), returnCurrentFinancial AS (
    SELECT d.SaleReturnID,
        CASE WHEN d.ImportedForAmg = 1 THEN p.SourceAmgCode ELSE p.SourceFenixCode END AS ProductCode,
        CONVERT(varchar(32), CASE WHEN d.ImportedForAmg = 1 THEN p.SourceAmgID ELSE p.SourceFenixID END, 2) AS SourceProductId,
        CASE WHEN CONVERT(float,CAST(item.Qty AS decimal(28,8))) = item.Qty
            THEN CAST(item.Qty AS decimal(28,8)) END AS Quantity, item.Amount AS LineAmountEur,
        item.ExchangeRateAmount AS EurDivisor
    FROM returnCurrencyDocuments d
    JOIN dbo.SaleReturnItem item ON item.SaleReturnID = d.SaleReturnID AND item.Deleted = 0
    LEFT JOIN dbo.OrderItem oi ON oi.ID = item.OrderItemID AND oi.Deleted = 0
    LEFT JOIN dbo.Product p ON p.ID = oi.ProductID
), returnCurrencyConfirmed AS (
    SELECT d.SaleReturnID FROM returnCurrencyDocuments d
    WHERE EXISTS (SELECT 1 FROM returnExpectedFinancial f WHERE f.SaleReturnID = d.SaleReturnID)
        AND NOT EXISTS (SELECT 1 FROM returnExpectedFinancial f WHERE f.SaleReturnID = d.SaleReturnID
            AND (f.SourceLineNumber IS NULL OR f.SourceLineNumber <= 0
                OR f.ProductCode IS NULL OR f.ProductCode <= 0 OR f.Quantity IS NULL OR f.Quantity <= 0
                OR f.LineAmountEur IS NULL OR DATALENGTH(f.SourceProductId) <> 64 OR DATALENGTH(f.SourceSaleId) <> 64
                OR TRY_CONVERT(binary(16),f.SourceProductId,2) IS NULL
                OR TRY_CONVERT(binary(16),f.SourceSaleId,2) IS NULL))
        AND NOT EXISTS (SELECT f.SourceLineNumber FROM returnExpectedFinancial f
            WHERE f.SaleReturnID = d.SaleReturnID GROUP BY f.SourceLineNumber HAVING COUNT_BIG(*) <> 1)
        -- Compare multisets, including counts: identical duplicate lines may never collapse.
        AND NOT EXISTS (
            SELECT f.ProductCode, f.SourceProductId, COUNT_BIG(*) FROM returnExpectedFinancial f
            WHERE f.SaleReturnID = d.SaleReturnID GROUP BY f.ProductCode, f.SourceProductId
            EXCEPT
            SELECT c.ProductCode, c.SourceProductId, COUNT_BIG(*) FROM returnExpectedCurrency c
            WHERE c.SaleReturnID = d.SaleReturnID GROUP BY c.ProductCode, c.SourceProductId)
        AND NOT EXISTS (
            SELECT c.ProductCode, c.SourceProductId, COUNT_BIG(*) FROM returnExpectedCurrency c
            WHERE c.SaleReturnID = d.SaleReturnID GROUP BY c.ProductCode, c.SourceProductId
            EXCEPT
            SELECT f.ProductCode, f.SourceProductId, COUNT_BIG(*) FROM returnExpectedFinancial f
            WHERE f.SaleReturnID = d.SaleReturnID GROUP BY f.ProductCode, f.SourceProductId)
        AND NOT EXISTS (
            SELECT f.ProductCode, f.SourceProductId, f.Quantity, f.LineAmountEur, f.EurDivisor, COUNT_BIG(*)
            FROM returnExpectedFinancial f WHERE f.SaleReturnID = d.SaleReturnID
            GROUP BY f.ProductCode, f.SourceProductId, f.Quantity, f.LineAmountEur, f.EurDivisor
            EXCEPT
            SELECT n.ProductCode, n.SourceProductId, n.Quantity, n.LineAmountEur, n.EurDivisor, COUNT_BIG(*)
            FROM returnCurrentFinancial n WHERE n.SaleReturnID = d.SaleReturnID
            GROUP BY n.ProductCode, n.SourceProductId, n.Quantity, n.LineAmountEur, n.EurDivisor)
        AND NOT EXISTS (
            SELECT n.ProductCode, n.SourceProductId, n.Quantity, n.LineAmountEur, n.EurDivisor, COUNT_BIG(*)
            FROM returnCurrentFinancial n WHERE n.SaleReturnID = d.SaleReturnID
            GROUP BY n.ProductCode, n.SourceProductId, n.Quantity, n.LineAmountEur, n.EurDivisor
            EXCEPT
            SELECT f.ProductCode, f.SourceProductId, f.Quantity, f.LineAmountEur, f.EurDivisor, COUNT_BIG(*)
            FROM returnExpectedFinancial f WHERE f.SaleReturnID = d.SaleReturnID
            GROUP BY f.ProductCode, f.SourceProductId, f.Quantity, f.LineAmountEur, f.EurDivisor)
)

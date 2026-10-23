CASE WHEN sr.SourceDocumentID IS NULL OR currency.SaleReturnID IS NOT NULL
                    THEN CAST(sri.Amount AS decimal(28,14)) ELSE 0 END

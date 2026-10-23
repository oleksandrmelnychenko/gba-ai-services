CASE WHEN sr.SourceDocumentID IS NOT NULL AND currency.SaleReturnID IS NULL
                    THEN 1 ELSE 0 END

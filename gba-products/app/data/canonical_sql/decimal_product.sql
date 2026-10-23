CASE WHEN CAST(TRY_CONVERT(decimal(16,8), {quantity}) AS float) = {quantity} THEN TRY_CONVERT(decimal(21,14), {price}) * TRY_CONVERT(decimal(16,8), {quantity}) END

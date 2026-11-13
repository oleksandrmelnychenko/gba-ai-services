"""SQL predicates shared by every solvency read, mirroring gba-server's definitions.

A confirmed sale is the native sales reports' fact (NativeSalesNetReportProjectionRepository
and siblings): a non-deleted, invoiced Sale that is not an accounting carrier, with non-deleted
valid lines of positive quantity, dated by its invoice instant (Sale.ChangedToInvoice, the
server's Sale.EffectiveDate for an invoiced sale). An open debt line is the credit-control one
(ClientCreditControlSnapshotRepository): non-deleted attachment and debt with a positive
remaining amount; a settled line kept with Total = 0 is not debt.

dbo.GetExchangedToEuroValue returns its input unchanged when a currency has no rate, so a debt
line is read only through debt_agreement(): a currency the function leaves unchanged (or a
missing one) keeps its lines out of every EUR sum, and unconverted_debt reports them.

Stored instants are UTC. Calendar arithmetic (days since, months since, month buckets) is done
on the Europe/Kyiv date of the instant, against a Kyiv as-of date.
"""
from __future__ import annotations

from app.core.history import SQL_BUSINESS_ZONE


def confirmed_sale(alias: str = "s") -> str:
    return (
        f"{alias}.Deleted = 0 AND {alias}.ChangedToInvoice IS NOT NULL "
        f"AND NOT EXISTS (SELECT 1 FROM dbo.DataSyncAccountingSourceTarget carrier "
        f"WHERE carrier.TargetEntityType = N'Sale' AND carrier.TargetEntityID = {alias}.ID "
        f"AND carrier.IsCurrent = 1 AND carrier.OwnershipType = 1)"
    )


def confirmed_line(alias: str = "oi") -> str:
    return f"{alias}.Deleted = 0 AND {alias}.IsValidForCurrentSale = 1 AND {alias}.Qty > 0"


def open_debt(attachment: str = "cid", debt: str = "d") -> str:
    return f"{attachment}.Deleted = 0 AND {debt}.Deleted = 0 AND {debt}.Total > 0"


def sold_at(alias: str = "s") -> str:
    return f"{alias}.ChangedToInvoice"


def local_date(column: str) -> str:
    return f"CAST({column} AT TIME ZONE 'UTC' AT TIME ZONE '{SQL_BUSINESS_ZONE}' AS date)"


def local_month(column: str) -> str:
    return f"CONVERT(char(7), {local_date(column)}, 126)"


def days_since(column: str, as_of: str) -> str:
    return f"DATEDIFF(day, {local_date(column)}, {as_of})"


def months_since(column: str, as_of: str) -> str:
    return f"DATEDIFF(month, {local_date(column)}, {as_of})"


FX_PROBE = 1000000


def convertible_currencies(fx_date: str) -> str:
    return (
        "(SELECT probe.ID FROM dbo.Currency probe WHERE probe.Code = N'EUR' "
        f"OR dbo.GetExchangedToEuroValue({FX_PROBE}, probe.ID, {fx_date}) <> {FX_PROBE})"
    )


def debt_agreement(fx_date: str, attachment: str = "cid", agreement: str = "a") -> str:
    return (
        f"JOIN dbo.Agreement {agreement} ON {agreement}.ID = {attachment}.AgreementID "
        f"JOIN {convertible_currencies(fx_date)} fx ON fx.ID = {agreement}.CurrencyID"
    )

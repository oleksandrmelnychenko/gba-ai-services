"""Current unpaid-document snapshot and the one overdue-debt rule of the cockpit.

The dashboard card, the head chart, the «Мої клієнти» column and debt follow-up tasks all
read classify() over documents(); only the task threshold (debt_min_amount) is task-specific.

Debt.Created is the persisted obligation date, not response time. Imported timestamps
need positive clock provenance; IsImported alone does not imply UTC or local time.
Agreement terms are current persisted terms; historical term changes are unavailable.

An opening balance is a document whose sale is an owned accounting carrier (the 1C balance
entered on 2024-12-31). It has no source lineage, so its clock cannot be verified; the stored
date is the only date there is and its uncertainty is hours against hundreds of days past
terms. It is counted as overdue on the stored date and reported separately as an opening
balance. It never raises a follow-up task: it needs an accounting decision, not a call.
An opening balance that the stored date and terms do not put past due stays unknown_date.
"""
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.core.money import cents, cents_decimal, decimal_value
from app.data.db import query

_KYIV = ZoneInfo('Europe/Kyiv')
_BUCKETS = [('0-30', 1, 30), ('31-60', 31, 60), ('61-90', 61, 90), ('90+', 91, None)]

_DOCUMENTS_SQL = """
SELECT d.ID AS debt_id, c.ID AS client_id, c.MainManagerID AS manager_id, d.Created AS obligation_at,
       CASE WHEN sale.IsImported = 0 THEN 'native_utc'
            WHEN sale.IsImported = 1 AND source_date.first_date = source_date.last_date
              AND ABS(DATEDIFF_BIG(millisecond, source_date.first_date, d.Created)) <= 4
              THEN 'imported_lineage_utc'
       END AS obligation_date_basis,
       CASE WHEN carrier.present = 1 THEN 1 ELSE 0 END AS opening_balance,
       CASE WHEN a.Deleted = 0 AND a.NumberDaysDebt >= 0 THEN a.NumberDaysDebt END AS term_days,
       currency.Code AS currency_code,
       CAST(d.Total AS decimal(30, 4)) AS amount_native,
       CASE WHEN fx.convertible = 1 THEN
         CAST(dbo.GetExchangedToEuroValue(d.Total, currency.ID, :fx_date) AS decimal(30, 4))
       END AS amount_eur
FROM dbo.ClientInDebt attachment
JOIN dbo.Debt d ON d.ID = attachment.DebtID AND d.Deleted = 0
JOIN dbo.Client c ON c.ID = attachment.ClientID AND c.Deleted = 0
LEFT JOIN dbo.Sale sale ON sale.ID = attachment.SaleID AND sale.Deleted = 0
OUTER APPLY (
  SELECT MIN(line.DestinationDocumentDate) AS first_date,
         MAX(line.DestinationDocumentDate) AS last_date
  FROM dbo.DataSyncImportedSaleSourceLine line
  WHERE line.LocalSaleID = sale.ID AND line.IsPresent = 1
) source_date
OUTER APPLY (
  SELECT TOP 1 1 AS present
  FROM dbo.DataSyncAccountingSourceTarget target
  WHERE target.TargetEntityType = N'Sale' AND target.TargetEntityID = sale.ID
    AND target.IsCurrent = 1 AND target.OwnershipType = 1
) carrier
LEFT JOIN dbo.Agreement a ON a.ID = attachment.AgreementID
LEFT JOIN dbo.Currency currency ON currency.ID = a.CurrencyID AND currency.Deleted = 0
LEFT JOIN (
  SELECT probe.ID,
         CASE WHEN probe.Code = 'EUR'
                OR dbo.GetExchangedToEuroValue(:fx_probe, probe.ID, :fx_date) <> :fx_probe
              THEN 1 ELSE 0 END AS convertible
  FROM dbo.Currency probe WHERE probe.Deleted = 0
) fx ON fx.ID = currency.ID
WHERE attachment.Deleted = 0 AND d.Total > 0 AND d.Created < :snapshot_end
  AND {scope}
"""

_FX_PROBE = 1000000


def documents(as_of: str, manager_id: int | None = None) -> list[dict]:
    """Every open debt document of the current client book, one row each.

    EUR is dbo.GetExchangedToEuroValue at the as-of date, the conversion gba-server and
    solvency use. The function returns its input when a currency has no rate, so a currency
    it leaves unchanged is reported with amount_eur NULL and never summed as EUR.
    """
    end = datetime.combine(date.fromisoformat(as_of) + timedelta(days=1), time.min)
    params = {'snapshot_end': end.replace(tzinfo=None), 'fx_date': as_of, 'fx_probe': _FX_PROBE}
    scope = 'c.MainManagerID IS NOT NULL'
    if manager_id is not None:
        scope = 'c.MainManagerID = :manager_id'
        params['manager_id'] = manager_id
    rows = query(_DOCUMENTS_SQL.format(scope=scope), params)
    if len({row['debt_id'] for row in rows}) != len(rows):
        raise ValueError('debt_document_attachment_ambiguous')
    return rows


_VERIFIED_BASES = ('native_utc', 'imported_lineage_utc')


def is_opening_balance(row: dict) -> bool:
    return bool(row.get('opening_balance')) and row.get('obligation_date_basis') not in _VERIFIED_BASES


def classify(row: dict, snapshot_day: date) -> tuple[str, int | None, int | None]:
    """(state, days_past_due, age_days); state is the single overdue rule of the cockpit.

    overdue: positive open amount, convertible to EUR, valid agreement terms, and the Kyiv
    obligation date plus terms is before the as-of day. The obligation clock is verified, or
    the document is an opening balance counted on its stored date. No age cap.
    unconverted, unknown_date and unknown_terms are reported, never summed.
    """
    if row.get('amount_eur') is None:
        return 'unconverted', None, None
    opening = is_opening_balance(row)
    if row.get('obligation_date_basis') not in _VERIFIED_BASES and not opening:
        return 'unknown_date', None, None
    created = row['obligation_at']
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    obligation_day = created.astimezone(_KYIV).date()
    if obligation_day > snapshot_day:
        return ('unknown_date' if opening else 'future'), None, None
    if row.get('term_days') is None:
        return ('unknown_date' if opening else 'unknown_terms'), None, None
    age_days = (snapshot_day - obligation_day).days
    days_past_due = age_days - int(row['term_days'])
    if opening and days_past_due < 1:
        return 'unknown_date', None, None
    return ('overdue' if days_past_due >= 1 else 'current'), days_past_due, age_days


def _unique(rows: list[dict]) -> None:
    if len({row['debt_id'] for row in rows}) != len(rows):
        raise ValueError('debt_document_attachment_ambiguous')


def fold_documents(rows: list[dict], as_of: str) -> dict:
    snapshot_day = date.fromisoformat(as_of)
    amounts = {name: Decimal(0) for name, _, _ in _BUCKETS}
    counts = {name: 0 for name, _, _ in _BUCKETS}
    unknown_amount = Decimal(0)
    unknown_count = 0
    unknown_date_amount = Decimal(0)
    unknown_date_count = 0
    opening_amount = Decimal(0)
    opening_count = 0
    unconverted: dict[str, Decimal] = defaultdict(Decimal)
    unconverted_count = 0
    _unique(rows)
    for row in rows:
        state, days_past_due, _ = classify(row, snapshot_day)
        if state == 'unconverted':
            unconverted_count += 1
            unconverted[str(row.get('currency_code') or 'unknown')] += decimal_value(row.get('amount_native'))
            continue
        amount = decimal_value(row['amount_eur'])
        if state == 'unknown_date':
            unknown_date_count += 1
            unknown_date_amount += amount
        elif state == 'unknown_terms':
            unknown_count += 1
            unknown_amount += amount
        elif state == 'overdue':
            if is_opening_balance(row):
                opening_count += 1
                opening_amount += amount
            for name, low, high in _BUCKETS:
                if days_past_due >= low and (high is None or days_past_due <= high):
                    amounts[name] += amount
                    counts[name] += 1
                    break
    amounts = {name: cents_decimal(value) for name, value in amounts.items()}
    return {
        'value_at_risk_eur': cents(sum(amounts.values(), Decimal(0))),
        'debt_aging': [{'bucket': name, 'amount_eur': cents(amounts[name]), 'count': counts[name]} for name, _, _ in _BUCKETS],
        'debt_opening_balance_count': opening_count,
        'debt_opening_balance_eur': cents(opening_amount),
        'debt_unknown_date_count': unknown_date_count,
        'debt_unknown_date_eur': cents(unknown_date_amount),
        'debt_unknown_terms_count': unknown_count,
        'debt_unknown_terms_eur': cents(unknown_amount),
        'debt_unconverted_count': unconverted_count,
        'debt_unconverted_native': [
            {'currency': code, 'amount': cents(total)} for code, total in sorted(unconverted.items())
        ],
    }


def fold_clients(rows: list[dict], as_of: str, *, with_unverified: bool = False) -> list[dict]:
    """Overdue documents per client: the «Мої клієнти» column and the debt follow-up source.

    overdue_amount, debt_lines, debt_ids and the two maxima cover every overdue document, so
    the column adds up to the card. opening_balance_amount/lines are the part of them that is
    an opening balance. The task_* fields leave opening balances out: they are what a
    follow-up task may be raised on, and task_days_past_terms lists each such document.
    with_unverified also returns clients whose only open documents have an unknown date,
    so the per-client unknown_date_amount adds up to the card's debt_unknown_date_eur.
    """
    snapshot_day = date.fromisoformat(as_of)
    _unique(rows)
    clients: dict[int, dict] = {}
    for row in sorted(rows, key=lambda item: item['debt_id']):
        state, days_past_due, age_days = classify(row, snapshot_day)
        if state not in ('overdue', 'unknown_date'):
            continue
        entry = clients.setdefault(int(row['client_id']), {
            'client_id': int(row['client_id']), 'manager_id': row.get('manager_id'),
            'debt_ids': [], 'overdue_amount': Decimal(0),
            'max_overdue_days': 0, 'max_days_past_terms': 0, 'debt_lines': 0,
            'opening_balance_amount': Decimal(0), 'opening_balance_lines': 0,
            'task_debt_ids': [], 'task_overdue_amount': Decimal(0),
            'task_max_overdue_days': 0, 'task_max_days_past_terms': 0, 'task_debt_lines': 0,
            'task_days_past_terms': [],
            'unknown_date_amount': Decimal(0), 'unknown_date_lines': 0,
        })
        amount = decimal_value(row['amount_eur'])
        if state == 'unknown_date':
            entry['unknown_date_amount'] += amount
            entry['unknown_date_lines'] += 1
            continue
        entry['debt_ids'].append(int(row['debt_id']))
        entry['overdue_amount'] += amount
        entry['max_overdue_days'] = max(entry['max_overdue_days'], age_days)
        entry['max_days_past_terms'] = max(entry['max_days_past_terms'], days_past_due)
        entry['debt_lines'] += 1
        if is_opening_balance(row):
            entry['opening_balance_amount'] += amount
            entry['opening_balance_lines'] += 1
            continue
        entry['task_debt_ids'].append(int(row['debt_id']))
        entry['task_overdue_amount'] += amount
        entry['task_max_overdue_days'] = max(entry['task_max_overdue_days'], age_days)
        entry['task_max_days_past_terms'] = max(entry['task_max_days_past_terms'], days_past_due)
        entry['task_debt_lines'] += 1
        entry['task_days_past_terms'].append(days_past_due)
    return [
        {**entry, 'debt_ids': ','.join(str(value) for value in entry['debt_ids']),
         'task_debt_ids': ','.join(str(value) for value in entry['task_debt_ids']),
         'overdue_amount': cents_decimal(entry['overdue_amount']),
         'opening_balance_amount': cents_decimal(entry['opening_balance_amount']),
         'task_overdue_amount': cents_decimal(entry['task_overdue_amount']),
         'unknown_date_amount': cents_decimal(entry['unknown_date_amount'])}
        for entry in clients.values()
        if entry['debt_lines'] or with_unverified
    ]


def task_candidates(clients: list[dict], min_amount: float, grace_days: int) -> list[dict]:
    """Clients a debt follow-up task may be raised for, in the task's own terms.

    Opening balances are left out, the amount must reach min_amount, and the oldest
    remaining document must be more than grace_days past terms.
    """
    minimum = decimal_value(min_amount)
    return [
        {'client_id': entry['client_id'], 'manager_id': entry.get('manager_id'),
         'debt_ids': entry['task_debt_ids'], 'overdue_amount': entry['task_overdue_amount'],
         'max_overdue_days': entry['task_max_overdue_days'],
         'max_days_past_terms': entry['task_max_days_past_terms'],
         'debt_lines': entry['task_debt_lines'],
         'days_past_terms': sorted(entry['task_days_past_terms'])}
        for entry in clients
        if entry['task_debt_lines'] and entry['task_overdue_amount'] >= minimum
        and entry['task_max_days_past_terms'] > grace_days
    ]


def overdue_clients(as_of: str, manager_id: int | None = None, *,
                    with_unverified: bool = False) -> list[dict]:
    return fold_clients(documents(as_of, manager_id), as_of, with_unverified=with_unverified)


def dashboards(as_of: str) -> dict[int, dict]:
    by_manager = defaultdict(list)
    for row in documents(as_of):
        by_manager[int(row['manager_id'])].append(row)
    return {manager: fold_documents(rows, as_of) for manager, rows in by_manager.items()}

from datetime import datetime
from decimal import Decimal

import pytest

from app.data.debt_snapshot import fold_clients, fold_documents


def debt(key, amount, created, terms=30):
    return {'debt_id': key, 'client_id': 7, 'amount_eur': amount, 'obligation_at': datetime.fromisoformat(created), 'term_days': terms, 'obligation_date_basis': 'native_utc'}


def test_each_document_uses_days_past_terms_instead_of_client_oldest_age():
    result = fold_documents([
        debt(1, 100, '2026-09-01T10:00:00'),  # 21 days past 30-day terms
        debt(2, 200, '2026-06-01T10:00:00'),  # 113 days past terms, same hypothetical client
        debt(3, 300, '2026-10-01T10:00:00'),  # not due yet
        debt(4, 400, '2026-09-22T10:00:00'),  # due today, not overdue
    ], '2026-10-22')
    assert result['value_at_risk_eur'] == 300
    assert result['debt_aging'][0] == {'bucket': '0-30', 'amount_eur': 100, 'count': 1}
    assert result['debt_aging'][3] == {'bucket': '90+', 'amount_eur': 200, 'count': 1}


def test_unknown_terms_are_separate_and_zero_terms_are_known():
    result = fold_documents([debt(1, 50, '2026-10-01T10:00:00', None), debt(2, 12, '2026-10-01T10:00:00', 0)], '2026-10-22')
    assert result['value_at_risk_eur'] == 12
    assert result['debt_unknown_terms_count'] == 1
    assert result['debt_unknown_terms_eur'] == 50


def test_kyiv_business_day_and_rounding_reconcile():
    # 21:30 UTC on Oct 21 is already Oct 22 in Kyiv; no overdue day yet.
    result = fold_documents([debt(1, 20, '2026-10-21T21:30:00', 0),
        debt(2, Decimal('0.005'), '2026-10-01T10:00:00', 0),
        debt(3, Decimal('0.005'), '2026-06-01T10:00:00', 0)], '2026-10-22')
    assert result['value_at_risk_eur'] == sum(row['amount_eur'] for row in result['debt_aging']) == 0.02


def test_missing_fx_is_excluded_and_reported_in_native_currency():
    missing = {**debt(2, None, '2026-10-01T10:00:00', 0), 'currency_code': 'CZK', 'amount_native': Decimal('2500')}
    result = fold_documents([debt(1, 100, '2026-10-01T10:00:00', 0), missing], '2026-10-22')
    assert result['value_at_risk_eur'] == 100
    assert result['debt_unconverted_count'] == 1
    assert result['debt_unconverted_native'] == [{'currency': 'CZK', 'amount': 2500.0}]
    assert fold_clients([debt(1, 100, '2026-10-01T10:00:00', 0), missing], '2026-10-22')[0]['overdue_amount'] == 100


def test_client_column_and_card_share_one_rule_without_age_cap():
    rows = [
        debt(1, 100, '2026-09-01T10:00:00'),
        debt(2, 200, '2024-06-01T10:00:00'),
        debt(3, 300, '2026-10-01T10:00:00'),
        debt(4, 50, '2026-09-01T10:00:00', None),
        {**debt(5, 70, '2026-09-01T10:00:00'), 'obligation_date_basis': None},
        {**debt(6, 40, '2026-08-01T10:00:00', 0), 'client_id': 8},
    ]
    card = fold_documents(rows, '2026-10-22')
    clients = {row['client_id']: row for row in fold_clients(rows, '2026-10-22')}
    assert card['value_at_risk_eur'] == 340
    assert sum(row['overdue_amount'] for row in clients.values()) == Decimal('340')
    assert clients[7]['debt_ids'] == '1,2' and clients[7]['debt_lines'] == 2
    assert clients[7]['max_days_past_terms'] == 843 and clients[7]['max_overdue_days'] == 873
    assert clients[8]['overdue_amount'] == Decimal('40.00')
    assert clients[7]['unknown_date_amount'] == Decimal('70.00') and clients[7]['unknown_date_lines'] == 1


def test_clients_with_only_unverified_documents_reach_the_column_but_never_a_task(monkeypatch):
    from app.data import debt_snapshot, signals_repository
    rows = [
        {**debt(1, 70, '2024-09-01T10:00:00'), 'client_id': 9, 'obligation_date_basis': None},
        {**debt(2, 30, '2026-09-01T10:00:00'), 'client_id': 10},
    ]
    assert [row['client_id'] for row in fold_clients(rows, '2026-10-22')] == [10]
    book = {row['client_id']: row for row in fold_clients(rows, '2026-10-22', with_unverified=True)}
    assert book[9]['overdue_amount'] == 0 and book[9]['unknown_date_amount'] == Decimal('70.00')
    card = fold_documents(rows, '2026-10-22')
    assert card['debt_unknown_date_eur'] == sum(row['unknown_date_amount'] for row in book.values())
    monkeypatch.setattr(debt_snapshot, 'documents', lambda as_of, manager_id=None: rows)
    assert [row['client_id'] for row in signals_repository.overdue_debts_for_manager(1, '2026-10-22', 10.0)] == [10]
    assert signals_repository.overdue_debts_for_manager(1, '2026-10-22', 50.0) == []


def opening(key, amount, created, terms=30, client=7):
    return {**debt(key, amount, created, terms), 'client_id': client,
            'obligation_date_basis': None, 'opening_balance': 1}


def test_opening_balances_count_as_overdue_on_the_stored_date_with_their_own_breakdown():
    rows = [
        debt(1, 100, '2026-09-01T10:00:00'),
        opening(2, 500, '2024-12-31T21:59:59', 14),
        opening(3, 250, '2015-11-01T10:00:00', 0, client=8),
        opening(4, 241.52, '2014-07-03T21:00:00', 9000, client=9),
        {**debt(5, 70, '2026-09-01T10:00:00'), 'obligation_date_basis': None},
    ]
    card = fold_documents(rows, '2026-11-13')
    assert card['value_at_risk_eur'] == 850
    assert card['debt_opening_balance_count'] == 2 and card['debt_opening_balance_eur'] == 750
    assert card['debt_unknown_date_count'] == 2 and card['debt_unknown_date_eur'] == 311.52
    assert card['debt_aging'][3] == {'bucket': '90+', 'amount_eur': 750, 'count': 2}
    book = {row['client_id']: row for row in fold_clients(rows, '2026-11-13', with_unverified=True)}
    assert sum(row['overdue_amount'] for row in book.values()) == Decimal('850')
    assert book[7]['overdue_amount'] == Decimal('600') and book[7]['debt_ids'] == '1,2'
    assert book[7]['opening_balance_amount'] == Decimal('500') and book[7]['opening_balance_lines'] == 1
    assert book[7]['task_overdue_amount'] == Decimal('100') and book[7]['task_debt_ids'] == '1'
    assert book[7]['max_days_past_terms'] == 668 and book[7]['task_max_days_past_terms'] == 43
    assert book[7]['unknown_date_amount'] == Decimal('70.00')
    assert book[8]['overdue_amount'] == Decimal('250') and book[8]['task_debt_lines'] == 0
    assert book[9]['overdue_amount'] == 0 and book[9]['unknown_date_amount'] == Decimal('241.52')
    assert sum(row['opening_balance_amount'] for row in book.values()) == Decimal('750')


def test_an_opening_balance_alone_never_raises_a_task_and_grace_holds_fresh_debt_back():
    from app.data.debt_snapshot import task_candidates
    rows = [
        opening(1, 5000, '2024-12-31T21:59:59', 14, client=8),
        opening(2, 900, '2024-12-31T21:59:59', 14, client=9),
        {**debt(3, 80, '2026-10-01T10:00:00', 30), 'client_id': 9},
        {**debt(4, 300, '2026-10-11T10:00:00', 30), 'client_id': 10},
        {**debt(5, 300, '2026-10-10T10:00:00', 30), 'client_id': 11},
        {**debt(6, 9, '2026-09-01T10:00:00', 30), 'client_id': 12},
        {**debt(7, 40, '2026-10-12T10:00:00', 30), 'client_id': 13},
        {**debt(8, 40, '2026-06-01T10:00:00', 30), 'client_id': 13},
    ]
    found = {row['client_id']: row for row in task_candidates(fold_clients(rows, '2026-11-13'), 10.0, 3)}
    assert set(found) == {9, 11, 13}
    assert found[9]['overdue_amount'] == Decimal('80') and found[9]['debt_ids'] == '3'
    assert found[9]['max_days_past_terms'] == 13 and found[9]['debt_lines'] == 1
    assert found[11]['max_days_past_terms'] == 4
    assert found[13]['days_past_terms'] == [2, 135] and found[13]['overdue_amount'] == Decimal('80')


def test_a_document_without_a_verified_clock_that_is_no_opening_balance_stays_unknown():
    row = {**debt(1, 70, '2024-09-01T10:00:00'), 'obligation_date_basis': None, 'opening_balance': 0}
    card = fold_documents([row], '2026-11-13')
    assert card['value_at_risk_eur'] == 0 and card['debt_unknown_date_eur'] == 70
    assert card['debt_opening_balance_count'] == 0


def test_documents_query_marks_opening_balances_by_the_accounting_carrier(monkeypatch):
    from app.data import debt_snapshot
    calls = []
    monkeypatch.setattr(debt_snapshot, 'query', lambda sql, params: calls.append(sql) or [])
    debt_snapshot.documents('2026-11-13')
    assert 'AS opening_balance' in calls[0]
    assert 'target.OwnershipType = 1' in calls[0] and 'target.IsCurrent = 1' in calls[0]


def test_documents_query_converts_at_the_as_of_date_through_the_database_function(monkeypatch):
    from app.data import debt_snapshot
    calls = []
    monkeypatch.setattr(debt_snapshot, 'query', lambda sql, params: calls.append((sql, params)) or [])
    debt_snapshot.overdue_clients('2026-10-22', 7)
    sql, params = calls[0]
    assert 'dbo.GetExchangedToEuroValue(d.Total, currency.ID, :fx_date)' in sql
    assert params['fx_date'] == '2026-10-22' and params['manager_id'] == 7
    assert 'c.MainManagerID = :manager_id' in sql
    assert 'SUM(d.Total)' not in sql and 'ISNULL(a.CurrencyID' not in sql
    assert 'DATEDIFF(day, d.Created' not in sql


def test_ambiguous_document_attachment_cannot_double_count():
    with pytest.raises(ValueError, match='attachment_ambiguous'):
        fold_documents([debt(1, 10, '2026-10-01T10:00:00', 0)] * 2, '2026-10-22')


@pytest.mark.parametrize('kind', ['debt', 'team'])
def test_failed_refresh_never_returns_expired_same_day_financial_cache(monkeypatch, kind):
    from app.api import main
    from app.core import history
    monkeypatch.setattr(main.time, 'monotonic', lambda: 10000)
    stale = {'at': 0, 'as_of': '2026-10-22', 'source_history_start': history.source_history_start().isoformat()}
    def fail(*args):
        raise ValueError('source unavailable')
    if kind == 'debt':
        stale['values'] = {1: {'value_at_risk_eur': 500}}
        monkeypatch.setattr(main, '_debt_dash_state', stale)
        monkeypatch.setattr(main.debt_snapshot, 'dashboards', fail)
        request = main._debt_dashboards_cached
    else:
        stale['value'] = {'team': {'total': 500}}
        monkeypatch.setattr(main, '_team_snap_state', stale)
        monkeypatch.setattr(main, '_build_team_snapshot', fail)
        request = main._team_snapshot_cached
    with pytest.raises(ValueError, match='source unavailable'):
        request('2026-10-22', allow_stale=True)


def test_imported_timestamp_requires_lineage_provenance():
    row = debt(1, 100, '2026-10-21T21:30:00', 0)
    row['obligation_date_basis'] = None
    result = fold_documents([row], '2026-10-22')
    assert result['value_at_risk_eur'] == 0
    assert result['debt_unknown_date_count'] == 1
    assert result['debt_unknown_date_eur'] == 100
    row['obligation_date_basis'] = 'imported_lineage_utc'
    assert fold_documents([row], '2026-10-22')['value_at_risk_eur'] == 0



def test_unknown_dates_are_separate_from_unknown_terms_and_known_aging():
    ambiguous = debt(1, 100, '2026-10-01T21:30:00', None)
    ambiguous['obligation_date_basis'] = None
    result = fold_documents([ambiguous, debt(2, 50, '2026-10-01T10:00:00', None),
        debt(3, 20, '2026-10-01T10:00:00', 0)], '2026-10-22')
    assert result['value_at_risk_eur'] == 20
    assert result['debt_unknown_terms_count'] == 1
    assert result['debt_unknown_terms_eur'] == 50
    assert result['debt_unknown_date_count'] == 1
    assert result['debt_unknown_date_eur'] == 100


def test_verified_future_business_day_is_excluded_but_ambiguous_clock_is_visible():
    future = debt(1, 100, '2026-10-22T22:00:00', 0)
    ambiguous = {**future, 'debt_id': 2, 'obligation_date_basis': None}
    result = fold_documents([future, ambiguous], '2026-10-22')
    assert result['value_at_risk_eur'] == 0
    assert result['debt_unknown_date_count'] == 1
    assert result['debt_unknown_date_eur'] == 100


def test_changed_invoice_definition_rejects_fresh_old_target_cache(monkeypatch):
    from app.api import main
    import time
    day = main._kyiv_today().isoformat()
    old = {"team": "old_created_basis"}
    new = {"team": "verified_invoice_basis"}
    monkeypatch.setattr(main, "_team_snap_state", {
        "at": time.monotonic(), "as_of": day,
        "source_history_start": main.history.source_history_start().isoformat(),
        "value": old,
    })
    calls = []
    monkeypatch.setattr(main, "_build_team_snapshot", lambda key: calls.append(key) or new)
    assert main._team_snapshot_cached(day) == new
    assert main._team_snapshot_cached(day) == new
    assert calls == [day]
    assert main._team_snap_state["metric_version"] == main._TEAM_METRIC_VERSION

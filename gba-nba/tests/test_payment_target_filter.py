"""Execute the payment aggregation against fixture rows, preserving its joins and filters."""
import sqlite3
from decimal import Decimal

from app.data import signals_repository as signals


def test_monthly_paid_excludes_canceled_deleted_and_other_manager_payments(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE Client(ID INTEGER, Deleted INTEGER, MainManagerID INTEGER);
        CREATE TABLE IncomePaymentOrder(ID INTEGER PRIMARY KEY, ClientID INTEGER, Deleted INTEGER,
                                        IsCanceled INTEGER, FromDate TEXT, Amount NUMERIC,
                                        CurrencyID INTEGER, EuroAmount NUMERIC);
        CREATE TABLE DataSyncAccountingSourceTarget(TargetEntityType TEXT, TargetEntityID INTEGER,
                                                    IsCurrent INTEGER, OwnershipType INTEGER);
        INSERT INTO Client VALUES (1,0,7),(2,0,8),(3,1,7);
        INSERT INTO IncomePaymentOrder
            (ClientID,Deleted,IsCanceled,FromDate,Amount,CurrencyID,EuroAmount) VALUES
          (1,0,0,'2026-10-01T09:00:00',500,1,12.50),
          (1,0,1,'2026-10-02T09:00:00',1500,1,30),
          (1,1,0,'2026-10-03T09:00:00',2000,1,40),
          (2,0,0,'2026-10-04T09:00:00',3000,1,60),
          (3,0,0,'2026-10-05T09:00:00',4000,1,80),
          (1,0,0,'2026-10-23T09:00:00',5000,1,100),
          (1,0,0,'2026-09-30T09:00:00',6000,1,120);
    """)
    # Only schema qualification and SQL Server's formatting/FX functions are adapted;
    # the real repository WHERE/JOIN/GROUP BY execute unchanged against the fixture.
    monkeypatch.setattr(signals, "query", lambda sql, params: [
        dict(row) for row in db.execute(sql.replace("dbo.", ""), params)
    ])
    assert signals.monthly_paid(7, "2026-10-01", "2026-10-23") == {"2026-10": Decimal('12.5')}
    db.execute("UPDATE IncomePaymentOrder SET EuroAmount=0 WHERE FromDate='2026-10-01T09:00:00'")
    import pytest
    with pytest.raises(ValueError, match='payment_eur_conversion_missing'):
        signals.monthly_paid(7, '2026-10-01', '2026-10-23')
    db.close()


def _payments(monkeypatch, rows: str):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(f"""
        CREATE TABLE Client(ID INTEGER, Deleted INTEGER, MainManagerID INTEGER);
        CREATE TABLE IncomePaymentOrder(ID INTEGER PRIMARY KEY, ClientID INTEGER, Deleted INTEGER,
                                        IsCanceled INTEGER, FromDate TEXT, Amount NUMERIC,
                                        CurrencyID INTEGER, EuroAmount NUMERIC);
        CREATE TABLE DataSyncAccountingSourceTarget(TargetEntityType TEXT, TargetEntityID INTEGER,
                                                    IsCurrent INTEGER, OwnershipType INTEGER);
        INSERT INTO Client VALUES (1,0,7);
        INSERT INTO IncomePaymentOrder(ID,ClientID,Deleted,IsCanceled,FromDate,Amount,CurrencyID,EuroAmount)
        VALUES {rows};
    """)
    monkeypatch.setattr(signals, "query", lambda sql, params: [
        dict(row) for row in db.execute(sql.replace("dbo.", ""), params)
    ])
    return db


def test_payment_registered_on_the_first_kyiv_day_belongs_to_that_month(monkeypatch):
    # A register dated 01.09 / 01.11 Kyiv is stored at 21:00 (summer) / 22:00 (winter) UTC of
    # the previous day; end-of-day documents are stored one second earlier.
    _payments(monkeypatch, """
        (1,1,0,0,'2026-08-31T21:00:00',100,1,10),
        (2,1,0,0,'2026-08-31T20:59:59',100,1,20),
        (3,1,0,0,'2026-10-31T22:00:00',100,1,40),
        (4,1,0,0,'2026-10-31T21:59:59',100,1,80),
        (5,1,0,0,'2026-11-13T21:59:59',100,1,160),
        (6,1,0,0,'2026-11-13T22:00:00',100,1,320),
        (7,1,0,0,'2026-07-31T20:59:59',100,1,640)
    """)
    assert signals.monthly_paid(7, "2026-08-01", "2026-11-14") == {
        "2026-08": Decimal("20"), "2026-09": Decimal("10"),
        "2026-10": Decimal("80"), "2026-11": Decimal("200"),
    }


def test_owned_accounting_payment_is_a_balance_carrier_not_cash(monkeypatch):
    db = _payments(monkeypatch, """
        (1,1,0,0,'2026-10-05T09:00:00',100,1,10),
        (2,1,0,0,'2026-10-06T09:00:00',100,1,20),
        (3,1,0,0,'2026-10-07T09:00:00',100,1,40)
    """)
    db.executescript("""
        INSERT INTO DataSyncAccountingSourceTarget VALUES ('IncomePaymentOrder',2,1,1),
            ('IncomePaymentOrder',3,1,2), ('Sale',1,1,1);
    """)
    assert signals.monthly_paid(7, "2026-10-01", "2026-11-01") == {"2026-10": Decimal("50")}

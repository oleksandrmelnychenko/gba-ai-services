"""Actual SQL acceptance on session-local temporary tables, with the deployed reader identity."""
from datetime import UTC, datetime

from sqlalchemy import text

from app.data import signals_repository as sig
from app.services import repeat_sales
from tests.test_purchase_history_sql import sql_fixture  # noqa: F401


def test_repeat_book_cycles_returns_drafts_merges_and_current_day(sql_fixture, monkeypatch):  # noqa: F811
    connection, run_base = sql_fixture
    connection.exec_driver_sql("""
        ALTER TABLE #Client ADD NetUID uniqueidentifier, Name nvarchar(100), FullName nvarchar(100),
            MobileNumber nvarchar(50), EmailAddress nvarchar(100);
    """)
    connection.exec_driver_sql("""
        CREATE TABLE #Product(ID bigint, NetUID uniqueidentifier, Name nvarchar(100), Deleted bit);
        CREATE TABLE #ProductAvailability(ProductID bigint, StorageID bigint, Amount float, Deleted bit);
        CREATE TABLE #Storage(ID bigint, Deleted bit, ForDefective bit);
        INSERT #Storage VALUES(1,0,0),(2,0,1),(3,1,0);
        INSERT #Client VALUES(30,1,0,NEWID(),N'Regular',NULL,NULL,NULL),
            (40,1,0,NEWID(),N'Seasonal',NULL,NULL,NULL),(50,1,0,NEWID(),N'No history',NULL,NULL,NULL),
            (60,1,0,NEWID(),N'Returned',NULL,NULL,NULL),(70,1,0,NEWID(),N'Ordered today',NULL,NULL,NULL),
            (80,1,0,NEWID(),N'Partial return',NULL,NULL,NULL),(90,1,0,NEWID(),N'Draft',NULL,NULL,NULL),
            (100,1,0,NEWID(),N'Merged',NULL,NULL,NULL);
        INSERT #ClientAgreement VALUES(30,30,0),(40,40,0),(50,50,0),(60,60,0),(70,70,0),
            (80,80,0),(90,90,0),(100,100,0);
    """)
    oid = 1000
    for cid in (30,40,60,70,80,90,100):
        dates = ['2026-09-10', '2026-09-24', '2026-10-08']
        if cid == 40:
            dates = ['2025-10-01', '2026-04-01', '2026-10-01']
        if cid == 70:
            dates += ['2026-10-22']
        connection.execute(text("INSERT #Product VALUES(:cid,NEWID(),N'Product',0)"), {'cid':cid})
        connection.execute(text("INSERT #ProductAvailability VALUES(:cid,1,:qty,0)"),
                           {'cid':cid,'qty':0 if cid==80 else 20})
        connection.execute(text("INSERT #ProductAvailability VALUES(:cid,2,1000,0),(:cid,3,1000,0)"),
                           {'cid':cid})
        for index, date in enumerate(dates):
            oid += 1
            connection.execute(text("INSERT #Order VALUES(:id,:cid,:dt,0,0)"),
                               {'id':oid,'cid':cid,'dt':date})
            connection.execute(text("""INSERT #Sale(ID,OrderID,IsImported,Deleted,IsMerged,ChangedToInvoice)
                                     VALUES(:id,:id,0,0,:merged,:invoice)"""),
                               {'id':oid,'merged':cid==100 and index<2,'invoice':None if cid==90 else date})
            connection.execute(text("""INSERT #OrderItem(ID,OrderID,ProductID,Qty,ReturnedQty,
                                     PricePerItem,Deleted,IsValidForCurrentSale)
                                   VALUES(:id,:id,:cid,10,:returned,10,0,1)"""),
                               {'id':oid,'cid':cid,'returned':10 if cid==60 else 6 if cid==80 else 0})
    def run(sql, params=None):
        for original, temporary in [('dbo.ProductAvailability','#ProductAvailability'),
                                    ('dbo.Product','#Product'),('dbo.Storage','#Storage')]:
            sql=sql.replace(original,temporary)
        return run_base(sql,params)
    monkeypatch.setattr(sig,'query',run)
    monkeypatch.setattr(sig,'_excluded',lambda *a:frozenset())
    monkeypatch.setattr(repeat_sales,'_latest_contacts',lambda *a:{})
    rows, products, churn = repeat_sales.read_snapshot(1,'2026-10-22')
    result = repeat_sales.enrich_and_page(1,'2026-10-22',rows,facts=(products,churn),
                                         now=datetime(2026,10,22,12,tzinfo=UTC))
    by_id={r['client_id']:r for r in result['clients']}
    assert by_id[30]['segments']==['reorder_due']
    assert by_id[40]['repeat_evidence']['products'][0]['cycle_days'] > 180
    assert 'reorder_due' not in by_id[40]['segments']
    assert by_id[50]['segments']==by_id[60]['segments']==by_id[90]['segments']==['new_client']
    assert by_id[70]['segments']==[]
    assert by_id[70]['repeat_evidence']['products'][0]['last_purchase_on']=='2026-10-22'
    assert by_id[80]['repeat_evidence']['products'][0]['last_quantity']==4
    assert by_id[80]['repeat_evidence']['products'][0]['available_qty']==0
    assert by_id[30]['repeat_evidence']['products'][0]['available_qty']==20
    assert by_id[100]['repeat_evidence']['products'][0]['purchase_count']==1
    assert by_id[100]['repeat_evidence']['products'][0]['cycle_days'] is None
    assert by_id[30]['turnover_eur']==300 and by_id[80]['turnover_eur']==120
    # Generator uses the very same cycle, with an explicit exclusive end boundary.
    generated={r['client_id']:r for r in sig.reorder_candidates_for_manager(1,'2026-10-22')}
    assert generated[30]['cycle_days']==by_id[30]['repeat_evidence']['products'][0]['cycle_days']
    assert 60 not in generated and 90 not in generated and 100 not in generated

    # Bound displayed evidence after all products contribute to counts, never before.
    for product_id in range(301,307):
        connection.execute(text("INSERT #Product VALUES(:pid,NEWID(),N'Additional',0)"), {'pid':product_id})
        for order_id in (1001,1002,1003):
            connection.execute(text("""INSERT #OrderItem(ID,OrderID,ProductID,Qty,ReturnedQty,
                PricePerItem,Deleted,IsValidForCurrentSale) VALUES(:id,:oid,:pid,1,0,1,0,1)"""),
                {'id':product_id*10000+order_id,'oid':order_id,'pid':product_id})
    capped_rows,capped_products,_=repeat_sales.read_snapshot(1,'2026-10-22')
    regular=next(row for row in capped_rows if row['client_id']==30)
    assert regular['retained_orders_cnt']==3 and regular['orders_cnt']==3
    assert regular['_due_count']==7
    assert len([row for row in capped_products if row['client_id']==30])==5

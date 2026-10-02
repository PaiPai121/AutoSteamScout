"""Regression cases: closed cohorts, unsold purchases and buyer-time confusion."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
import pytest

from autoscout.orders import AccountOrder, PurchaseLine, parse_steampy_orders, parse_steampy_request_orders
from autoscout.products import ProductIdentity
from autoscout.repository import Repository
from autoscout.settings import Settings
from autoscout.titles import TitleCatalog
from autoscout.turnover import build_turnover_report, candidate_history, public_report
from autoscout.domain import PricingPolicy
from autoscout.web import create_app

NOW = datetime(2026, 10, 2, 4, tzinfo=timezone.utc)
NOW_TEXT = NOW.isoformat()


def purchase(identifier, when, count=1, title='Game', product_id='101', refunded=0):
    return AccountOrder('sonkwo', identifier, title, when, 'completed', count, Decimal('10') * count,
                        lines=(PurchaseLine(1,title,(),count,Decimal('10'),refunded,
                                            Decimal('10') * refunded,'platform' if refunded else '',product_id),))


def sale(identifier, when, title='Game', product_id='201', gross='12', **changes):
    return AccountOrder('steampy',identifier,title,when,'sold',1,Decimal(gross),Decimal('0.36'),
                        Decimal(gross)-Decimal('0.36'),product_id=product_id,**changes)


def save(repo, orders, **kwargs):
    repo.replace_account_orders(orders,source_coverage=('ordinary','request'),
                                source_synced_at={k:NOW_TEXT for k in ('sonkwo','ordinary','request')},**kwargs)


def snapshots():
    left = ProductIdentity('sonkwo','101',('Game',),'https://www.sonkwo.hk/sku/101','base','cn',detail_checked=True)
    right = ProductIdentity('steampy','201',('Game',),'https://steampy.com/cdkDetail?name=cn&gameId=201','base','cn',detail_checked=True)
    return left.snapshot(),right.snapshot()


def test_unsold_units_and_immature_quick_sales_cannot_inflate_cohort_rate(tmp_path):
    repo=Repository(tmp_path/'cohorts.sqlite3')
    # FIFO would allocate a same-title second sale to the older purchase. A separate
    # title makes the rapidly sold immature unit's cohort unambiguous.
    orders=[purchase('mature','2026-09-01T04:00:00+00:00',2),
            purchase('recent','2026-10-01T04:00:00+00:00',title='Young',product_id='102'),
            sale('sold-old','2026-09-03 12:00:00'),
            sale('sold-young','2026-10-01 12:01:00',title='Young',product_id='202')]
    save(repo,orders)
    report=repo.turnover_report(now=NOW)
    totals=report['summary']
    assert totals['bought_units']==3 and totals['matched_sold_units']==2 and totals['unmatched_units']==1
    for days in ('7','30'):
        assert totals['windows'][days]=={'eligible_units':2,'matched_in_window':1,'immature_units':1,'matched_percent':'50.00'}
    assert totals['unmatched_over_30d']==1 and totals['unmatched_cost']=='10.00'
    assert totals['oldest_unmatched_days']==31.0
    assert '_units' not in report['products'][0]
    assert sum(w['sold_units'] for w in report['weeks'])==2


def test_platform_stock_time_and_purchase_time_are_not_buyer_order_creation(tmp_path):
    """Sanitized live shape: buyer creates an order 32 seconds before completion,
    but the platform has held the specific inventory record since March.
    """
    raw={'id':'s1','gameId':'201','createTime':'2026-08-19 00:46:37','txTime':'2026-08-19 00:47:09',
         'txPrice':'12.00','fee':'0.36','steamGame':{'id':'201','gameName':'Game'},
         'steamKeySaleDetail':{'cdKey':'SECRET-MUST-NOT-PERSIST','createTime':'2026-03-22 11:44:24'}}
    parsed=parse_steampy_orders([{'id':'s1','gameId':'201','createTime':raw['createTime']}],[raw])[0]
    assert parsed.product_id=='201' and parsed.stock_created_at=='2026-03-22 11:44:24'
    repo=Repository(tmp_path/'stock-time.sqlite3')
    save(repo,[purchase('b1','2026-03-22T03:31:57+00:00'),parsed])
    report=repo.turnover_report(now=NOW)
    assert report['summary']['completed_median_days']==149.55
    assert report['summary']['stock_to_sale_median_days']==149.54
    assert report['summary']['stock_time_samples']==1
    assert report['summary']['windows']['30']['matched_in_window']==0
    assert repo.saved_account_orders('steampy')[0].stock_created_at==parsed.stock_created_at
    assert 'SECRET-MUST-NOT-PERSIST' not in repo.path.read_bytes().decode('utf-8',errors='ignore')


def test_refunds_failures_and_before_2025_are_not_purchase_turnover(tmp_path):
    repo=Repository(tmp_path/'scope.sqlite3')
    save(repo,[purchase('partial','2026-09-01T04:00:00+00:00',3,refunded=1),
               purchase('old','2024-12-30T04:00:00+00:00'),
               AccountOrder('steampy','failed','Game','2026-09-02 12:00:00','status_40',1,None),
               sale('sold','2026-09-08 12:00:00')])
    report=repo.turnover_report(now=NOW)
    assert report['summary']['bought_units']==2
    assert report['summary']['windows']['7']['matched_percent']=='50.00'
    assert report['summary']['unmatched_units']==1


def test_earliest_source_snapshot_caps_followup_and_preserves_stale_flag(tmp_path):
    repo=Repository(tmp_path/'freshness.sqlite3')
    orders=[purchase('b','2026-09-01T04:00:00+00:00'),sale('s','2026-10-01 12:00:00')]
    repo.replace_account_orders(orders,source_coverage=('ordinary','request'),
                                source_synced_at={'sonkwo':'2026-09-29T04:00:00+00:00','ordinary':NOW_TEXT,'request':NOW_TEXT})
    report=repo.turnover_report(now=NOW)
    assert report['as_of']=='2026-09-29T04:00:00+00:00' and report['stale']
    assert report['summary']['matched_sold_units']==0
    assert report['summary']['windows']['30']['eligible_units']==0
    assert report['summary']['oldest_unmatched_days']==28


def test_missing_request_coverage_does_not_claim_complete_sell_through(tmp_path):
    repo=Repository(tmp_path/'partial.sqlite3')
    repo.replace_account_orders([purchase('b','2026-09-01T04:00:00+00:00'),sale('s','2026-09-03 12:00:00')])
    report=repo.turnover_report(now=NOW)
    assert report['coverage_complete'] is False
    assert report['summary']['windows']['30']['matched_in_window']==1
    assert report['summary']['windows']['30']['matched_percent'] is None


def test_candidate_history_requires_variant_ids_and_full_official_names(tmp_path):
    repo=Repository(tmp_path/'identity.sqlite3')
    save(repo,[purchase('b','2026-09-01T04:00:00+00:00'),sale('s','2026-09-03 12:00:00')])
    report=repo.turnover_report(now=NOW,include_units=True)
    left,right=snapshots()
    def history(a=left,b=right):return candidate_history(a,b,report,TitleCatalog(),Decimal('10'),PricingPolicy())
    own=history()
    assert own['status']=='personal_history' and own['own_sales_30d']==1
    assert own['completed_median_days']==2
    assert history(b={**right,'product_id':'202'})['status']=='no_history'
    assert history(a={**left,'names':['Game Deluxe Edition']})['status']=='no_history'
    assert history(a={**left,'names':['Different Game']})['status']=='no_history'
    assert history(b={**right,'names':['Game Deluxe Edition']})['status']=='no_history'
    assert history(b={**right,'market_region':'ru'})['status']=='no_history'


def test_recent_personal_prices_are_a_scenario_not_public_volume(tmp_path):
    repo=Repository(tmp_path/'prices.sqlite3')
    save(repo,[purchase('b','2026-09-01T04:00:00+00:00',4),
               *(sale(f's{i}',f'2026-09-{10+i:02d} 12:00:00',gross=str(12+i)) for i in range(3)),
               AccountOrder('steampy','request','Game','2026-09-15 12:00:00','sold',1,None,None,Decimal('9.70'),
                            channel='request',product_id='201')])
    left,right=snapshots()
    own=candidate_history(left,right,repo.turnover_report(now=NOW,include_units=True),TitleCatalog(),Decimal('10'),PricingPolicy())
    assert own['own_sales_30d']==4 and own['gross_price_samples_30d']==3
    assert own['observed_lower_quartile_gross_30d']=='12.00'
    assert own['observed_price_profit']=='1.52'
    assert 'recent_sales_30d' not in own and 'estimated_sell_days' not in own


def test_request_ids_are_whitelisted_and_conflicting_order_product_ids_fail():
    raw={'id':'rq','gameId':'201','gameName':'Game','createTime':'2026-09-02 12:00:00',
         'updateTime':'2026-09-02 12:01:00','txStatus':'20','txPrice':'10','cdKey':'SECRET'}
    parsed=parse_steampy_request_orders([raw])[0]
    assert parsed.product_id=='201' and parsed.stock_created_at is None
    with pytest.raises(Exception,match='编号'):
        parse_steampy_orders([{'id':'s','gameId':'201','createTime':'2026-09-02 12:00:00'}],
                            [{'id':'s','gameId':'202','steamGame':{'id':'201'}}])


def test_invalid_stock_record_time_does_not_change_accounting_or_capital_duration(tmp_path):
    repo=Repository(tmp_path/'invalid-date.sqlite3')
    save(repo,[purchase('b','2026-09-01T04:00:00+00:00'),sale('s','2026-09-03 12:00:00',stock_created_at='2026-10-05 12:00:00')])
    report=repo.turnover_report(now=NOW)
    assert report['summary']['completed_median_days']==2
    assert report['summary']['stock_to_sale_median_days'] is None
    assert any('库存记录时间无效' in issue for issue in report['issues'])
    assert repo.account_order_summary()['steampy']['sale_net']=='11.64'


def test_turnover_api_returns_all_purchase_cohorts_even_when_inventory_is_paged(tmp_path):
    settings=Settings(tmp_path)
    repo=Repository(settings.database_path)
    save(repo,[purchase(f'b{i}','2026-09-01T04:00:00+00:00',title=f'Game {i}',product_id=str(100+i)) for i in range(37)])
    with TestClient(create_app(settings,repo)) as client:
        assert len(client.get('/api/account-orders/inventory').json()['rows'])==30
        report=client.get('/api/account-orders/turnover').json()
        assert report['summary']['bought_units']==37 and len(report['products'])==37
        assert all('_units' not in p for p in report['products'])

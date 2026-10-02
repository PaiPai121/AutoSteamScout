const $ = id => document.getElementById(id);
let token = '';
let entry = null;
let lastStatus = '';
let cruiseEnabled = false;
let accountPage = 1;
let inventoryPage = 1;
let unconfirmedPage = 1;
let accountRefreshSeq = 0;
let matchSnapshot = null;
let bankBillId = null;
let productEntry = null;
let productEvidenceEntry = null;
const accountPageSize = 50;
const inventoryPageSize = 30;
const unconfirmedPageSize = 50;

const verdictLabels = {
  opportunity: '求购价也达标', price_only: '仅有挂价差', low_margin: '利润不足', needs_review: '待核实',
  no_quote: '无报价', error: '采集错误'
};
const stateLabels = {
  purchased: '已买入', listed: '已上架', sold: '已售出待回款',
  settled: '已结算', refunded: '已退款', written_off: '已核销'
};
const actions = {
  purchased: [['listed', '记录上架'], ['refunded', '记录退款'], ['written_off', '核销损失']],
  listed: [['sold', '记录售出'], ['refunded', '记录退款'], ['written_off', '核销损失']],
  sold: [['settled', '记录到账'], ['written_off', '核销损失']]
};

function money(value) { return value == null ? '—' : `¥${Number(value).toFixed(2)}`; }
function orderTime(value) {
  if (!value) return '—';
  return /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? new Date(value).toLocaleString('zh-CN') : value;
}
function textCell(row, value) {
  const cell = document.createElement('td');
  cell.textContent = value == null ? '—' : String(value);
  row.appendChild(cell);
  return cell;
}
function tag(value, label) {
  const span = document.createElement('span');
  span.className = `tag ${value}`;
  span.textContent = label;
  return span;
}
function link(url, label) {
  const a = document.createElement('a');
  if (url && /^https:\/\//i.test(url)) {
    a.href = url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
  }
  a.textContent = label;
  return a;
}
function notice(message) { $('notice').textContent = message || ''; }
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {'Content-Type': 'application/json', 'X-Scout-Token': token, ...(options.headers || {})}
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || `请求失败：${response.status}`);
  return data;
}

async function refreshStatus() {
  const status = await api('/api/status');
  $('status-pill').textContent = status.status === 'running' ? '扫描中' :
    status.status === 'idle' ? '待命' : status.status === 'completed' ? '已完成' :
    status.status === 'completed_with_warnings' ? '完成，有警告' :
    status.status === 'cancelled' ? '已取消' : '失败';
  $('status-pill').className = `pill ${status.status}`;
  const alert = $('scan-alert');
  alert.hidden = status.status !== 'failed';
  alert.textContent = status.status === 'failed' ?
    `上次扫描失败：${status.error || '请查看扫描日志和账号会话状态'}` : '';
  $('progress-stage').textContent = status.stage === 'idle' ? '尚未扫描' : status.stage;
  $('progress-counts').textContent = `发现 ${status.discovered} · 已核价 ${status.processed} · 候选 ${status.opportunities}` +
    (status.elapsed_seconds == null ? '' : ` · ${status.elapsed_seconds} 秒`);
  $('cancel-scan').disabled = status.status !== 'running';
  $('scan-form').querySelector('button[type=submit]').disabled = status.status === 'running';
  if (status.error) notice(status.error);
  else if (status.last_warning) notice(`警告：${status.last_warning}`);
  if (lastStatus === 'running' && status.status !== 'running') await refreshAll();
  lastStatus = status.status;
}

async function refreshCruise() {
  const [cruise, stats] = await Promise.all([api('/api/cruise'), api('/api/statistics')]);
  cruiseEnabled = cruise.enabled;
  $('toggle-cruise').textContent = cruise.enabled ? '暂停巡航' : '恢复巡航';
  const next = cruise.next_run_at ? new Date(cruise.next_run_at).toLocaleString('zh-CN') : '暂无';
  const base = cruise.enabled ? `自动巡航已开启 · 每 ${Math.round(cruise.interval_seconds / 60)} 分钟` : '自动巡航已暂停';
  $('cruise-state').textContent = `${base} · 下次扫描：${next}` +
    (cruise.current_keyword == null ? '' : ` · 当前关键词：${cruise.current_keyword || '全场'}`) +
    (cruise.last_error ? ` · 最近错误：${cruise.last_error}` : '');
  $('run-count').textContent = Object.values(stats.run_counts).reduce((sum, count) => sum + count, 0);
  $('unique-offers').textContent = stats.unique_offers;
  $('latest-opportunities').textContent = stats.latest_verdicts.opportunity || 0;
  $('latest-estimate').textContent = money(stats.latest_estimated_profit);
  $('demand-statistics').textContent = `另有 ${stats.latest_verdicts.price_only || 0} 款仅有挂价差`;
  $('scan-trend').textContent = stats.daily.map(day =>
    `${day.day}：${day.runs} 轮 / ${day.processed || 0} 件 / ${day.opportunities || 0} 个候选`
  ).join('　　');
}

function renderMatches(snapshot) {
  const ready = snapshot && snapshot.ready;
  const counts = snapshot?.counts || {};
  $('match-candidate').textContent = ready ? counts.candidate || 0 : '—';
  $('match-fifo').textContent = ready ? counts.fifo || 0 : '—';
  $('match-unmatched').textContent = ready ? (counts.review || 0) + (counts.unmatched || 0) : '—';
  $('match-spread').textContent = ready ? money(snapshot.matched_order_spread) : '—';
  const state = $('match-state').value;
  const rows = (snapshot?.rows || []).filter(item => state === 'all' || item.status === state);
  $('match-count').textContent = ready ? `显示 ${rows.length} / ${snapshot.rows.length} 笔成交` : '尚未完成订单同步';
  const body = $('match-rows'); body.replaceChildren();
  if (!rows.length) {
    const row = document.createElement('tr');
    const cell = textCell(row, ready ? '此状态下没有成交订单' : '尚未完成订单同步');
    cell.colSpan = 10; cell.className = 'empty'; body.appendChild(row);
    return;
  }
  for (const item of rows) {
    const row = document.createElement('tr');
    const sale = document.createElement('td');
    sale.textContent = item.sale_title;
    const saleId = document.createElement('small'); saleId.className = 'order-id';
    saleId.textContent = `${item.sale_channel === 'request' ? '求购' : '普通'} · ${item.sale_order_id}`;
    sale.appendChild(saleId); row.appendChild(sale);
    textCell(row, orderTime(item.sale_time));
    const buy = document.createElement('td');
    buy.textContent = item.buy_title || '—';
    if (item.buy_order_id) {
      const buyId = document.createElement('small'); buyId.className = 'order-id';
      buyId.textContent = `订单 ${item.buy_order_id} · 配对 1 件`;
      buy.appendChild(buyId);
    }
    row.appendChild(buy);
    const stateCell = document.createElement('td');
    const labels = {candidate: '单一来源', fifo: '先购先销', review: '未能配对', unmatched: '无相符购买'};
    stateCell.appendChild(tag(item.status, labels[item.status] || item.status)); row.appendChild(stateCell);
    textCell(row, money(item.buy_unit_cost)).className = item.buy_unit_cost == null ? '' : 'flow-out';
    textCell(row, money(item.sale_gross)).className = 'flow-in';
    textCell(row, money(item.sale_fee));
    textCell(row, money(item.sale_net)).className = 'flow-in';
    textCell(row, money(item.order_stage_spread));
    textCell(row, item.reason).className = 'match-reason';
    body.appendChild(row);
  }
}

function renderInventory(snapshot, requestIncluded) {
  const ready = snapshot.ready;
  const totals = snapshot.summary;
  $('inventory-bought').textContent = ready ? totals.bought_units : '—';
  $('inventory-products').textContent = ready ? `${totals.product_count} 种杉果商品` : '每件购买商品单独列出';
  $('inventory-sold').textContent = ready ? totals.sold_units : '—';
  $('inventory-unsold').textContent = ready ? totals.unsold_units : '—';
  $('inventory-unsold-cost').textContent = ready ? `买入成本 ${money(totals.unsold_cost)}` : '买入成本仍单列';
  $('inventory-loss').textContent = ready ? totals.loss_units : '—';
  $('inventory-spread').textContent = ready ? money(totals.order_stage_spread) : '—';
  $('inventory-count').textContent = ready ? `符合筛选 ${snapshot.total} 件购买商品` : '尚未同步进货';
  $('inventory-warning').textContent = ready ?
    (`统计范围：${snapshot.report_start} 起。` +
      (totals.unallocated_sales ? `${totals.unallocated_sales} 笔成功销售尚未分配买入成本，需单独核对。` :
        requestIncluded ? '已同步的普通与求购成交均已分配买入商品；挂单和库存尚未逐件核对。' :
          '当前服务尚未纳入求购成交；挂单和库存尚未逐件核对。') +
      (totals.gross_unknown_sales ? ` ${totals.gross_unknown_sales} 笔求购成交只有净收入，原价与费用待核实。` : '')) : '尚未同步进货';
  $('inventory-page').textContent = `第 ${snapshot.page} / ${snapshot.pages} 页`;
  $('inventory-prev').disabled = snapshot.page <= 1;
  $('inventory-next').disabled = snapshot.page >= snapshot.pages;
  const body = $('inventory-rows'); body.replaceChildren();
  if (!snapshot.rows.length) {
    const row = document.createElement('tr');
    const cell = textCell(row, ready ? '没有符合筛选条件的购买商品' : '尚未同步购买商品');
    cell.colSpan = 9; cell.className = 'empty'; body.appendChild(row);
    return;
  }
  for (const unit of snapshot.rows) {
    const row = document.createElement('tr');
    const name = textCell(row, unit.title);
    const buyId = document.createElement('small'); buyId.className = 'order-id';
    const purchaseQuantity = unit.purchase_quantity;
    buyId.textContent = `订单 ${unit.purchase_order_id}` +
      (purchaseQuantity > 1 ?
        ` · 同单买入 ${purchaseQuantity} 件 · 本行第 ${unit.purchase_unit_no} 件` :
        purchaseQuantity === 1 ? ' · 买入 1 件' : ' · 本行 1 件');
    name.appendChild(buyId);
    textCell(row, orderTime(unit.purchase_time));
    const state = document.createElement('td');
    state.appendChild(tag(unit.status === 'sold' ? unit.allocation : 'not-counted',
      unit.status === 'sold' ? `已配对${unit.sale_channel === 'request' ? '求购' : '普通'}成交` :
        requestIncluded ? '未找到对应成交' : '普通成交中未找到'));
    if (unit.reason) {
      const reason = document.createElement('small'); reason.className = 'order-note';
      reason.textContent = unit.reason; state.appendChild(reason);
    }
    row.appendChild(state);
    textCell(row, money(unit.unit_cost)).className = 'flow-out';
    textCell(row, money(unit.sale_gross));
    textCell(row, money(unit.sale_fee));
    textCell(row, money(unit.sale_net)).className = unit.status === 'sold' ? 'flow-in' : '';
    const spread = textCell(row, money(unit.order_stage_spread));
    spread.className = unit.order_stage_spread == null ? '' :
      Number(unit.order_stage_spread) < 0 ? 'inventory-loss' : 'inventory-gain';
    const sale = textCell(row, unit.sale_order_id || '—');
    if (unit.sale_time) {
      const time = document.createElement('small'); time.className = 'order-note';
      time.textContent = orderTime(unit.sale_time); sale.appendChild(time);
    }
    if (unit.sale_title && unit.sale_title !== unit.title) {
      const title = document.createElement('small'); title.className = 'order-note';
      title.textContent = unit.sale_title; sale.appendChild(title);
    }
    body.appendChild(row);
  }
}

function renderUnconfirmed(snapshot) {
  $('unconfirmed-count').textContent = `共 ${snapshot.total} 条记录`;
  $('unconfirmed-page').textContent = `第 ${snapshot.page} / ${snapshot.pages} 页`;
  $('unconfirmed-prev').disabled = snapshot.page <= 1;
  $('unconfirmed-next').disabled = snapshot.page >= snapshot.pages;
  const body = $('unconfirmed-rows'); body.replaceChildren();
  if (!snapshot.rows.length) {
    const row = document.createElement('tr');
    const cell = textCell(row, '目前没有未作为有效成交的订单记录');
    cell.colSpan = 5; cell.className = 'empty'; body.appendChild(row); return;
  }
  for (const order of snapshot.rows) {
    const row = document.createElement('tr');
    textCell(row, `${order.channel === 'request' ? '求购' : '普通'} · ${order.order_id}`).className = 'order-id';
    textCell(row, orderTime(order.occurred_at));
    textCell(row, order.title);
    const status = document.createElement('td');
    const label = order.status === 'refunded' ? '已退款' :
      order.status.startsWith('status_') ?
        `平台码 ${order.status.slice(7)} · ${order.channel === 'request' ? '未计入成交' : '未在成功单'}` :
        order.status;
    status.appendChild(tag('not-counted', label));
    if (order.wallet_checked) {
      const note = document.createElement('small'); note.className = 'order-note';
      note.textContent = order.wallet_credit_amount == null ? '已同步钱包流水中未见该订单号入账' :
        `该订单号曾入账 ${money(order.wallet_credit_amount)}，需核对状态`;
      status.appendChild(note);
    }
    row.appendChild(status);
    const related = document.createElement('td');
    if (order.later_same_product_sale_count) {
      related.textContent = `同款后续 ${order.later_same_product_sale_count} 笔成功单（不同订单号）`;
      for (const sale of order.later_same_product_sales) {
        const note = document.createElement('small'); note.className = 'order-note related-sale';
        note.textContent = `${sale.order_id} · ${orderTime(sale.occurred_at)} · ${money(sale.amount)}`;
        related.appendChild(note);
      }
    } else related.textContent = '尚未找到后续同款成功单';
    row.appendChild(related); body.appendChild(row);
  }
}

function renderFinanceOverview(summary) {
  $('finance-purchase-spent').textContent = summary.orders_ready ? money(summary.purchase_spent) : '—';
  $('finance-purchase-details').textContent = summary.orders_ready ?
    `原实付 ${money(summary.purchase_paid)} − 退款商品 ${money(summary.purchase_refunds)} · 有效进货 ${summary.purchase_units} 件` : '尚未同步购买';
  $('finance-sale-net').textContent = summary.orders_ready ? money(summary.sale_net) : '—';
  $('finance-sale-details').textContent = `${summary.sale_count} 笔普通与求购成交，已扣订单费`;
  $('finance-withdrawals').textContent = summary.payouts_ready ? money(summary.withdrawal_debits) : '—';
  $('finance-withdrawal-details').textContent = `${summary.withdrawal_count} 笔钱包提现扣款，到账另核对`;
  $('finance-withdrawal-fees').textContent = summary.payouts_ready ? money(summary.withdrawal_fees) : '—';
  $('finance-bank-received').textContent = !summary.payouts_ready ? '—' :
    summary.bank_confirmed_count ? money(summary.bank_confirmed) : '待核对';
  $('finance-bank-details').textContent = `已核对 ${summary.bank_confirmed_count} / ${summary.withdrawal_count} 笔 · 仍有 ${summary.bank_pending_count} 笔待核对`;
  $('finance-note').textContent = `已剔除 ${summary.refunded_units} 件退款商品。` +
    (summary.refunds_pending_platform_verification ? '其中有按你的确认修正的退款，保留确认依据；平台会话恢复后继续核验。' : '') +
    '提现扣款不是银行到账证明；这里不把未经核对的回款计作已实现利润。';
  renderWalletReconciliation(summary.wallet);
}

function renderWalletReconciliation(wallet) {
  const ready = wallet?.ready;
  $('finance-wallet-other-debits').textContent = ready ? money(wallet.other_debits) : '—';
  $('finance-wallet-balance').textContent = wallet?.available_balance != null ? money(wallet.available_balance) : '未读取';
  $('finance-wallet-balance-details').textContent = wallet?.balance_at ?
    `待入账 ${money(wallet.pending_balance)} · 核对于 ${orderTime(wallet.balance_at)}` : '同步钱包流水后读取官方余额';
  $('finance-wallet-gap').textContent = wallet?.balance_difference != null ? money(wallet.balance_difference) : '待核对';
  $('finance-wallet-gap-details').textContent = wallet?.balance_difference === '0.00' ?
    '平台可用余额与已同步流水净额一致' : '可用余额减全部已同步流水净额';
  $('wallet-equation').textContent = ready ?
    `销售入账 ${money(wallet.sale_credits)} − 提现 ${money(wallet.withdrawals)} − 提现费 ${money(wallet.withdrawal_fees)} − 其他支出 ${money(wallet.other_debits)} + 其他入账 ${money(wallet.other_credits)} = 本期流水净变动 ${money(wallet.period_net)}` : '尚未同步钱包流水';
  let note = ready ? '收支按 2025 年起的钱包流水计算；可用余额来自平台，按全部已同步钱包历史核对。' : '';
  if (ready && wallet.pre_period_net !== '0.00') {
    note += `2025 年前流水净变动 ${money(wallet.pre_period_net)}，不计入本期收入；全部流水净额 ${money(wallet.history_net)}。`;
  }
  if (ready && !wallet.sale_credits_verified) {
    note += `销售订单与钱包入账尚未逐笔全部核对，合计相差 ${money(wallet.sale_credit_difference)}；以上按钱包实际流水列示。`;
  }
  if (wallet?.balance_difference != null && wallet.balance_difference !== '0.00') {
    note += '余额仍有差额，可能涉及期初资金或未同步流水；不会自动计作利润或费用。';
  }
  $('wallet-reconciliation-note').textContent = note;
  const rows = wallet?.other_movements || [];
  $('wallet-other-count').textContent = `(${rows.length} 笔)`;
  const body = $('wallet-other-rows'); body.replaceChildren();
  if (!rows.length) {
    const row = document.createElement('tr'); const cell = textCell(row, ready ? '本期暂无其他钱包收支' : '尚未同步钱包');
    cell.colSpan = 5; cell.className = 'empty'; body.appendChild(row);
  }
  for (const bill of rows) {
    const row = document.createElement('tr');
    textCell(row, orderTime(bill.occurred_at)); textCell(row, bill.label);
    textCell(row, bill.direction === 'debit' ? money(Math.abs(Number(bill.amount))) : '—').className = 'flow-out';
    textCell(row, bill.direction === 'credit' ? money(bill.amount) : '—').className = 'flow-in';
    textCell(row, bill.tx_id || '平台未提供'); body.appendChild(row);
  }
}

function renderPurchaseRefunds(refunds) {
  $('refund-count').textContent = `(${refunds.reduce((sum, line) => sum + line.refunded_quantity, 0)} 件)`;
  const body = $('refund-rows'); body.replaceChildren();
  if (!refunds.length) {
    const row = document.createElement('tr'); const cell = textCell(row, '暂无已确认退款商品');
    cell.colSpan = 5; cell.className = 'empty'; body.appendChild(row); return;
  }
  for (const line of refunds) {
    const row = document.createElement('tr'); const product = textCell(row, line.title);
    const id = document.createElement('small'); id.className = 'order-id'; id.textContent = `订单 ${line.order_id}`;
    product.appendChild(id); textCell(row, orderTime(line.purchase_time));
    textCell(row, line.refunded_quantity); textCell(row, money(line.refund_amount));
    textCell(row, line.refund_source === 'platform' ? '平台退款成功记录' : line.evidence || '用户确认退款成功');
    body.appendChild(row);
  }
}

async function refreshAccountOrders() {
  const sequence = ++accountRefreshSeq;
  const params = new URLSearchParams({
    platform: $('account-platform').value, state: $('account-state').value,
    query: $('account-query').value.trim(), page: String(accountPage),
    page_size: String(accountPageSize)
  });
  const inventoryParams = new URLSearchParams({
    state: $('inventory-state').value, query: $('inventory-query').value.trim(),
    page: String(inventoryPage), page_size: String(inventoryPageSize)
  });
  const unconfirmedParams = new URLSearchParams({
    platform: 'steampy', state: 'not_counted',
    page: String(unconfirmedPage), page_size: String(unconfirmedPageSize)
  });
  const [status, summary, ledger, matches, inventory, unconfirmed, finance, refunds, turnover] = await Promise.all([
    api('/api/account-orders/status'), api('/api/account-orders/summary'),
    api(`/api/account-orders/ledger?${params}`), api('/api/account-orders/reconciliation'),
    api(`/api/account-orders/inventory?${inventoryParams}`),
    api(`/api/account-orders/ledger?${unconfirmedParams}`),
    api('/api/finance/overview'), api('/api/account-orders/refunds'), api('/api/account-orders/turnover').catch(error => {
      $('turnover-as-of').textContent = `周转统计暂时不可读：${error.message}`; return null;
    })
  ]);
  if (sequence !== accountRefreshSeq) return;
  matchSnapshot = matches;
  renderMatches(matches);
  renderFinanceOverview(finance);
  renderPurchaseRefunds(refunds);
  if (turnover) renderTurnover(turnover);
  if (ledger.page > ledger.pages || inventory.page > inventory.pages ||
      unconfirmed.page > unconfirmed.pages) {
    accountPage = Math.min(accountPage, ledger.pages);
    inventoryPage = Math.min(inventoryPage, inventory.pages);
    unconfirmedPage = Math.min(unconfirmedPage, unconfirmed.pages);
    return refreshAccountOrders();
  }
  const requestIncluded = Array.isArray(summary.source_coverage) &&
    summary.source_coverage.includes('request');
  renderInventory(inventory, requestIncluded);
  renderUnconfirmed(unconfirmed);
  $('sync-orders').disabled = status.status === 'running';
  const last = summary.last_synced_at ? new Date(summary.last_synced_at).toLocaleString('zh-CN') : '尚无完整同步';
  const sourceTimes = summary.source_synced_at || {};
  const sourceDetails = Object.entries({sonkwo: '杉果', ordinary: '普通订单', request: '求购订单'})
    .filter(([key]) => sourceTimes[key])
    .map(([key, label]) => `${label} ${orderTime(sourceTimes[key])}`).join(' · ');
  const sourceWarnings = summary.sync_warnings || (status.status === 'completed_with_warnings' ?
    (status.warnings || []) : []);
  $('account-order-state').textContent = `最近订单快照发布：${last}` +
    (sourceDetails ? ` · 来源时间：${sourceDetails}` : '') +
    (status.status === 'running' ? ` · 正在同步：${status.stage} · 已过 ${status.elapsed_seconds} 秒` : '') +
    (sourceWarnings.length ? ` · 来源警告：${sourceWarnings.join('；')}` : '') +
    (status.status === 'failed' ? ` · 最近失败：${status.error}` : '');
  const unlinked = summary.unlinked_wallet_credits || {count: 0, amount: '0.00', types: {}};
  const unlinkedDetails = !('unlinked_wallet_credits' in summary) ?
    '当前服务尚未加载未关联钱包流水核对。' : summary.wallet_synced_at ?
    `另有 ${unlinked.count} 条正向钱包流水（合计 ${money(unlinked.amount)}，类型 ${Object.keys(unlinked.types).join('、') || '无'}）未关联当前成交订单。` :
    '钱包流水尚未同步，无法核对未关联入账。';
  const unclassified = summary.unclassified_wallet_movements ||
    {count: 0, debits: '0.00', credits: '0.00', types: {}};
  const movementDetails = summary.wallet_synced_at && unclassified.count ?
    `另有 ${unclassified.count} 条未归因钱包流水（流出 ${money(unclassified.debits)}、流入 ${money(unclassified.credits)}，类型 ${Object.keys(unclassified.types).join('、')}）。` : '';
  const creditProblems = summary.wallet_synced_at && requestIncluded ?
    `成交单钱包入账缺失 ${summary.missing_wallet_credit_count || 0} 笔、金额不符 ${summary.mismatched_wallet_credit_count || 0} 笔。` : '';
  $('account-coverage').textContent = requestIncluded ?
    `已纳入 SteamPy 普通与求购订单；挂单和库存尚未逐件核对。${unlinkedDetails}${creditProblems}${movementDetails}“未找到对应成交”表示已同步销售记录中没有可对应的成交，上架和库存状态仍待确认。` :
    `当前服务尚未纳入求购订单；挂单和库存也尚未逐件核对。${unlinkedDetails}“普通订单未配对”不能证明未上架或未卖出。`;
  const hasSnapshot = Boolean(summary.last_synced_at);
  $('sonkwo-orders').textContent = hasSnapshot ? summary.sonkwo.completed : '—';
  $('sonkwo-units').textContent = hasSnapshot ? summary.sonkwo.units : '—';
  $('sonkwo-spent').textContent = hasSnapshot ? money(summary.sonkwo.spent) : '—';
  $('steampy-sold').textContent = hasSnapshot ? summary.steampy.sold : '—';
  $('steampy-sold-details').textContent = requestIncluded ?
    `${summary.steampy.ordinary_sold} 普通 · ${summary.steampy.request_sold} 求购` : '当前仅普通订单';
  $('steampy-gross').textContent = hasSnapshot ? money(summary.steampy.gross) : '—';
  $('steampy-fees').textContent = hasSnapshot ? money(summary.steampy.fees) : '—';
  $('steampy-request-net').textContent = hasSnapshot && requestIncluded ?
    money(summary.steampy.request_net) : '—';
  $('steampy-net').textContent = hasSnapshot ? money(summary.steampy.sale_net) : '—';
  $('steampy-net-details').textContent = requestIncluded ?
    '普通与求购成交；另核对提现到账' : '当前仅普通成交；另核对提现到账';
  $('steampy-other').textContent = hasSnapshot ? summary.steampy.other : '—';
  const otherCodes = summary.steampy.other_statuses || {};
  $('steampy-other-details').textContent = hasSnapshot ? Object.entries(otherCodes).map(([status, count]) =>
    `${status.startsWith('request_') ? `求购状态码 ${status.replace('request_status_', '')}` :
      status.startsWith('status_') ? `普通状态码 ${status.slice(7)}` : status}：${count} 单`
  ).join(' · ') || '无' : '平台状态待核实';
  $('account-profit-note').textContent = summary.profit_note;
  $('ledger-count').textContent = hasSnapshot ? `符合条件 ${ledger.total} 单` : '尚未同步';
  $('ledger-page').textContent = `第 ${ledger.page} / ${ledger.pages} 页`;
  $('ledger-prev').disabled = ledger.page <= 1;
  $('ledger-next').disabled = ledger.page >= ledger.pages;
  const body = $('account-ledger-rows'); body.replaceChildren();
  if (!ledger.rows.length) {
    const row = document.createElement('tr');
    const cell = textCell(row, hasSnapshot ? '没有符合筛选条件的订单' : '尚未同步订单');
    cell.colSpan = 9; cell.className = 'empty'; body.appendChild(row);
    return;
  }
  for (const order of ledger.rows) {
    const row = document.createElement('tr');
    const source = document.createElement('td');
    source.textContent = order.platform === 'sonkwo' ? '杉果购买' :
      `SteamPy ${order.channel === 'request' ? '求购' : '普通'}订单`;
    const id = document.createElement('small'); id.className = 'order-id';
    id.textContent = order.order_id; source.appendChild(id); row.appendChild(source);
    textCell(row, orderTime(order.occurred_at));
    const products = textCell(row, order.title);
    if (order.platform === 'sonkwo' && ((order.purchase_lines || []).length > 1 ||
        (order.purchase_lines || []).some(line => line.refunded_quantity > 0))) {
      products.replaceChildren();
      for (const line of order.purchase_lines) {
        const detail = document.createElement('small'); detail.className = 'order-note';
        detail.textContent = `${line.title} × ${line.quantity} 件 · 单件 ${money(line.unit_cost)}` +
          (line.refunded_quantity ? ` · 已退款 ${line.refunded_quantity} 件（${money(line.refund_amount)}）` : '');
        products.appendChild(detail);
      }
    }
    const state = document.createElement('td');
    if (order.platform === 'sonkwo') {
      const purchaseState = order.purchase_display_status || order.status;
      state.appendChild(tag(purchaseState, ({completed: '已完成购买', refunded: '商品已全部退款',
        partially_refunded: '部分商品已退款'})[purchaseState] || `平台状态：${purchaseState}`));
    } else if (order.status === 'sold') {
      state.appendChild(tag('sold', '成功成交'));
      if (order.wallet_checked) {
        const note = document.createElement('small'); note.className = 'order-note';
        note.textContent = order.wallet_credit_matches_net ?
          `钱包同订单号入账金额匹配：${money(order.wallet_credit_amount)}` :
          order.wallet_credit_amount == null ? '已同步钱包流水中未见此订单号入账' :
          `钱包入账 ${money(order.wallet_credit_amount)}，与订单扣费后收入不符`;
        state.appendChild(note);
      }
    } else {
      const suffix = order.status.startsWith('status_') ? ` · 平台码 ${order.status.slice(7)}` : '';
      state.appendChild(tag('not-counted', order.status === 'refunded' ?
        '成功单已退款' : order.channel === 'request' ?
          `求购订单未计入成交${suffix}` : `此订单号不在成功单中${suffix}`));
      if (order.wallet_checked) {
        const note = document.createElement('small'); note.className = 'order-note';
        note.textContent = order.wallet_credit_amount == null ? '已同步钱包流水中未见此订单号入账' :
          `此订单号钱包曾入账 ${money(order.wallet_credit_amount)}，需核对状态`;
        state.appendChild(note);
      }
      if (order.later_same_product_sale_count) {
        const note = document.createElement('small'); note.className = 'order-note related-sale';
        const examples = order.later_same_product_sales.map(sale =>
          `${sale.order_id}（${orderTime(sale.occurred_at)}，${money(sale.amount)}）`).join('、');
        const extra = order.later_same_product_sale_count > order.later_same_product_sales.length ? '等' : '';
        note.textContent = `同款之后有 ${order.later_same_product_sale_count} 笔成功成交，订单号不同：${examples}${extra}`;
        state.appendChild(note);
      }
    }
    row.appendChild(state);
    textCell(row, order.quantity);
    const purchase = order.platform === 'sonkwo' && ['completed', 'refunded'].includes(order.status);
    const sale = order.platform === 'steampy' && order.status === 'sold';
    const outflow = textCell(row, purchase ? money(order.purchase_net_spent ?? order.amount) : '—');
    outflow.className = purchase ? 'flow-out' : '';
    if (purchase && Number(order.purchase_refund_amount) > 0) {
      const note = document.createElement('small'); note.className = 'order-note';
      note.textContent = `原实付 ${money(order.amount)} − 退款 ${money(order.purchase_refund_amount)}`;
      outflow.appendChild(note);
    }
    textCell(row, sale ? money(order.amount) : '—').className = sale ? 'flow-in' : '';
    textCell(row, sale ? money(order.fee) : '—');
    textCell(row, sale ? money(order.net_amount) : '—').className = sale ? 'flow-in' : '';
    body.appendChild(row);
  }
}

async function refreshPayouts() {
  const [status, summary] = await Promise.all([
    api('/api/payouts/status'), api('/api/payouts')
  ]);
  $('sync-payouts').disabled = status.status === 'running';
  const last = summary.last_synced_at ? orderTime(summary.last_synced_at) : '尚未同步';
  $('payout-state').textContent = `最近完整同步：${last} · 钱包流水 ${summary.wallet_bill_count} 条` +
    (status.status === 'running' ? ` · 正在同步：${status.stage} · 已过 ${status.elapsed_seconds} 秒` : '') +
    (status.status === 'failed' ? ` · 最近失败：${status.error}` : '');
  const ready = summary.ready;
  $('payout-debits').textContent = ready ? money(summary.wallet_debits) : '—';
  $('payout-fees').textContent = ready ? money(summary.wallet_fees) : '—';
  $('payout-bank').textContent = !ready ? '—' :
    summary.rows.some(row => row.bank_received != null) ? money(summary.bank_confirmed) : '待核对';
  $('payout-pending').textContent = ready ? summary.awaiting_bank_check : '—';
  $('payout-count').textContent = ready ? `${summary.withdrawal_count} 笔钱包记录` : '钱包记录';
  const body = $('payout-rows'); body.replaceChildren();
  if (!summary.rows.length) {
    const row = document.createElement('tr');
    const cell = textCell(row, ready ? '钱包历史里没有提现扣款记录' : '尚未同步钱包');
    cell.colSpan = 7; cell.className = 'empty'; body.appendChild(row); return;
  }
  for (const payout of summary.rows) {
    const row = document.createElement('tr');
    textCell(row, orderTime(payout.occurred_at));
    textCell(row, money(payout.wallet_debit)).className = 'flow-out';
    textCell(row, money(payout.wallet_fee)).className = 'flow-out';
    const bank = textCell(row, payout.bank_received == null ? '—' :
      `${money(payout.bank_received)} · ${payout.bank_received_at}`);
    if (payout.bank_received != null) bank.className = 'flow-in';
    textCell(row, money(payout.bank_difference));
    const state = document.createElement('td');
    state.appendChild(tag(payout.status, payout.status === 'manual_receipt' ?
      '银行到账已手工核对' : '银行到账待核对'));
    row.appendChild(state);
    const action = document.createElement('td');
    const button = document.createElement('button'); button.type = 'button';
    button.className = 'ghost small-button';
    button.textContent = payout.status === 'manual_receipt' ? '更正到账' : '录入到账';
    button.addEventListener('click', () => {
      bankBillId = payout.bill_id;
      $('bank-subtitle').textContent = `平台提现扣款 ${money(payout.wallet_debit)}，请照银行流水录入实际到账。`;
      $('bank-amount').value = payout.bank_received || '';
      $('bank-date').value = payout.bank_received_at || '';
      $('bank-note').value = payout.bank_note || '';
      $('bank-dialog').showModal();
    });
    action.appendChild(button);
    if (payout.status === 'manual_receipt') {
      const clear = document.createElement('button');
      clear.type = 'button'; clear.className = 'ghost small-button';
      clear.textContent = '撤销核对';
      clear.addEventListener('click', async () => {
        if (!confirm('撤销这笔银行到账核对记录？')) return;
        try {
          await api(`/api/payouts/${encodeURIComponent(payout.bill_id)}/receipt`, {method: 'DELETE'});
          await Promise.all([refreshPayouts(), refreshAccountOrders()]);
        } catch (error) { $('payout-state').textContent = error.message; }
      });
      action.appendChild(clear);
    }
    row.appendChild(action); body.appendChild(row);
  }
}

const productMethodLabels = {official_names: '官方名称对应', configured_alias: '按已有别名核对',
  confirmed_product_pair: '已确认具体商品对应', steam_app_candidate: 'Steam 应用一致，激活包待确认',
  steam_package: 'Steam 应用及激活包对应', unconfirmed: '尚未确认'};
const contentLabels = {base: '游戏本体', dlc: 'DLC', soundtrack: '原声', unknown: '未提供'};
const editionLabels = {standard: '未标注特别版本，按标准版核对', deluxe: '豪华版', gold: '黄金版', ultimate: '终极版',
  complete: '完整版', directors_cut: '导演剪辑版', goty: '年度版', definitive: '最终版', remastered: '高清版',
  remake: '重制版', royal: '皇家版', premium: '高级版', mixed: '版本标记冲突'};

function productInfo(container, product, heading = '') {
  if (heading) { const strong = document.createElement('strong'); strong.textContent = heading; container.appendChild(strong); }
  if (!product) { const note = document.createElement('p'); note.textContent = '此旧结果缺少商品详情快照，需要重新扫描。'; container.appendChild(note); return; }
  const name = document.createElement('p'); name.appendChild(link(product.url, product.names.join(' / '))); container.appendChild(name);
  for (const text of [
    `商品编号：${product.product_id}`,
    `版本：${product.editions?.map(value => editionLabels[value] || value).join(' / ') || '未识别'} · 内容：${contentLabels[product.content_type] || product.content_type}`,
    `市场分类：${product.market_region === 'cn' ? '国区' : product.market_region || '未提供'}（逐 Key 激活限制需另核实）`,
    `Steam AppID：${product.steam_app_id || '平台未提供'} · 激活包编号：${product.steam_package_id || '平台未提供'}`,
    product.detail_checked ? '原商品详情已核对编号与名称' : '来自本次官方搜索结果，取价时还会核对原详情'
  ]) { const item = document.createElement('div'); item.className = 'product-info'; item.textContent = text; container.appendChild(item); }
  if (product.steam_app_id) container.appendChild(link(`https://store.steampowered.com/app/${product.steam_app_id}/`, '查看 Steam 应用页'));
}

function productConflict(left, right) {
  if (!left?.detail_checked || left.detail_issue) return '杉果原商品详情尚未核对，不能保存对应关系。';
  if (!right || right.detail_issue) return 'SteamPy 商品信息不可用。';
  if (left.editions?.length !== 1 || right.editions?.length !== 1 || left.editions[0] === 'mixed' || right.editions[0] === 'mixed') return '官方名称的版本信息相互冲突，需先解决来源问题。';
  if (left.editions[0] !== right.editions[0]) return '两边版本不同，不能保存为同款。';
  if (left.content_type !== 'unknown' && right.content_type !== 'unknown' && left.content_type !== right.content_type) return '两边内容类型不同，不能将本体与 DLC 等内容对应。';
  if (left.steam_app_id && right.steam_app_id && left.steam_app_id !== right.steam_app_id) return '两边 Steam AppID 不同。';
  if (left.steam_package_id && right.steam_package_id && left.steam_package_id !== right.steam_package_id) return '两边 Steam 激活包编号不同。';
  if (left.market_region && right.market_region && left.market_region !== right.market_region) return '两边市场区域分类不同。';
  return '';
}

function updateProductChoice() {
  if (!productEntry) return;
  const product = productEntry.market_candidates.find(item => item.identity?.product_id === $('product-choice').value);
  $('product-market').replaceChildren(); productInfo($('product-market'), product?.identity);
  const conflict = productConflict(productEntry.matching?.offer, product?.identity);
  $('product-conflict').hidden = !conflict; $('product-conflict').textContent = conflict;
  $('product-save').disabled = Boolean(conflict);
  $('product-confirmed').checked = false;
}

function openProductEntry(result) {
  productEntry = result;
  $('product-offer').replaceChildren(); productInfo($('product-offer'), result.matching?.offer, '杉果购买商品');
  $('product-choice').replaceChildren();
  for (const product of result.market_candidates || []) {
    if (!product.identity) continue;
    const option = document.createElement('option'); option.value = product.identity.product_id;
    option.textContent = `${product.title}${product.alternate_titles?.length ? ` / ${product.alternate_titles.join(' / ')}` : ''} · ${option.value}`;
    $('product-choice').appendChild(option);
  }
  $('product-note').value = ''; $('product-feedback').textContent = '';
  updateProductChoice(); $('product-dialog').showModal();
}

function openProductEvidence(result) {
  productEvidenceEntry = result;
  const matching = result.matching;
  $('product-evidence-method').textContent = matching ?
    `${productMethodLabels[matching.method] || '待确认'}${matching.offer_name && matching.market_name ? `：${matching.offer_name} ↔ ${matching.market_name}` : ''} · 核对时间 ${orderTime(matching.checked_at)}` :
    '旧结果仅保存了名称判断，需要重新扫描补全详情依据。';
  $('product-evidence-offer').replaceChildren(); productInfo($('product-evidence-offer'), matching?.offer, '杉果');
  $('product-evidence-market').replaceChildren();
  if (matching?.market) productInfo($('product-evidence-market'), matching.market, 'SteamPy');
  else {
    const pending = document.createElement('p'); pending.className = 'product-info';
    pending.textContent = `尚未确认具体市场商品。${result.reason}`; $('product-evidence-market').appendChild(pending);
  }
  $('product-evidence-limit').textContent = matching?.limitation || '';
  $('product-evidence-review').hidden = !result.market_candidates?.some(item => item.identity) || !matching?.offer;
  $('product-evidence-dialog').showModal();
}

async function refreshProductMappings() {
  const mappings = await api('/api/product-mappings');
  const body = $('product-mapping-rows'); body.replaceChildren();
  if (!mappings.length) { const row = document.createElement('tr'); textCell(row, '暂无确认关系').colSpan = 4; body.appendChild(row); return; }
  for (const mapping of mappings) {
    const row = document.createElement('tr');
    for (const product of [mapping.offer, mapping.market]) {
      const cell = document.createElement('td'); cell.appendChild(link(product.url, product.names.join(' / ')));
      const id = document.createElement('small'); id.className = 'order-note'; id.textContent = `商品编号 ${product.product_id}`;
      cell.appendChild(id); row.appendChild(cell);
    }
    textCell(row, `${orderTime(mapping.confirmed_at)}${mapping.note ? ` · ${mapping.note}` : ''}`);
    const cell = document.createElement('td'); const button = document.createElement('button');
    button.type = 'button'; button.className = 'small-button ghost'; button.textContent = '撤销对应关系';
    button.addEventListener('click', async () => {
      button.disabled = true;
      try { await api(`/api/product-mappings/${mapping.sonkwo_id}`, {method: 'DELETE'}); await refreshProductMappings(); notice('对应关系已撤销，下次扫描重新核对。'); }
      catch (error) { notice(error.message); button.disabled = false; }
    });
    cell.appendChild(button); row.appendChild(cell); body.appendChild(row);
  }
}

function signed(value, suffix = '') {
  return value == null ? '未知' : `${Number(value) > 0 ? '+' : ''}${value}${suffix}`;
}

function holdingDays(value) { return value == null ? '—' : `${Number(value).toFixed(2)} 天`; }
function cohortText(window) {
  if (!window?.eligible_units) return '观察天数不足';
  return `${window.matched_in_window} / ${window.eligible_units} 件` + (window.matched_percent == null ? ' · 来源未齐' : ` · ${window.matched_percent}%`);
}

function renderTurnover(report) {
  const summary = report.summary;
  $('turnover-as-of').textContent = report.ready ? `订单数据截至 ${orderTime(report.as_of)}${report.stale ? ' · 订单快照超过 1 小时' : ''}` : '请同步历史订单';
  $('turnover-median').textContent = report.ready ? holdingDays(summary.completed_median_days) : '—';
  for (const days of [7, 30]) {
    const window = summary.windows[String(days)];
    $(`turnover-${days}d`).textContent = window.matched_percent == null ? '—' : `${window.matched_percent}%`;
    $(`turnover-${days}d-sample`).textContent = `${window.matched_in_window} / ${window.eligible_units} 件；${window.immature_units} 件尚未观察满 ${days} 天`;
  }
  $('turnover-old').textContent = report.ready ? `${summary.unmatched_over_30d} 件` : '—';
  $('turnover-old-cost').textContent = `全部未配对成本 ${money(summary.unmatched_cost)}`;
  $('turnover-note').textContent = `${report.note} ${report.issues.join('；')}`;
  const body = $('turnover-rows'); body.replaceChildren();
  for (const product of report.products) {
    const row = document.createElement('tr');
    textCell(row, product.title); textCell(row, `${product.bought_units} / ${product.matched_sold_units} / ${product.unmatched_units} 件`);
    textCell(row, holdingDays(product.completed_median_days));
    textCell(row, cohortText(product.windows['7'])); textCell(row, cohortText(product.windows['30']));
    textCell(row, product.last_sale_at ? orderTime(product.last_sale_at) : '未配对成交');
    textCell(row, product.unmatched_units ? `最长 ${holdingDays(product.oldest_unmatched_days)} · ${money(product.unmatched_cost)}` : '全部已配对');
    const stock = textCell(row, holdingDays(product.stock_to_sale_median_days));
    const note = document.createElement('small'); note.className = 'order-note';
    note.textContent = `${product.stock_time_samples} 件有记录；含未上架或暂停时间`;
    stock.appendChild(note); body.appendChild(row);
  }
  if (!report.products.length) { const row = document.createElement('tr'); textCell(row, '尚无完整进货样本').colSpan = 8; body.appendChild(row); }
  drawOwnSalesChart(report.weeks);
}

function drawOwnSalesChart(weeks) {
  const target = $('turnover-week-chart'); target.replaceChildren();
  if (!weeks.length) return;
  const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 780 130'); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', '最近 12 周自己的已配对成交件数');
  const max = Math.max(1, ...weeks.map(week => week.sold_units));
  weeks.forEach((week, index) => {
    const x = 12 + index * 64, height = week.sold_units / max * 70, rect = document.createElementNS(ns, 'rect');
    for (const [key, value] of Object.entries({x, y: 90 - height, width: 42, height: Math.max(1, height), fill: week.partial ? '#efc46a' : '#50d0ad'})) rect.setAttribute(key, value);
    const title = document.createElementNS(ns, 'title'); title.textContent = `${week.start} 起 · ${week.sold_units} 件 · 净收入 ${money(week.sale_net)}${week.partial ? ' · 未满一周' : ''}`;
    rect.appendChild(title); svg.appendChild(rect);
    for (const [text, y] of [[String(week.sold_units), 80 - height], [week.start.slice(5), 112]]) {
      const label = document.createElementNS(ns, 'text'); label.setAttribute('x', x + 4); label.setAttribute('y', y); label.setAttribute('fill', '#aabfc0'); label.textContent = text; svg.appendChild(label);
    }
  }); target.appendChild(svg);
}

function ownHistoryText(history) {
  if (!history || history.status !== 'personal_history') return '自己的同款样本：尚无按商品编号和完整名称核对的进货记录。';
  return `自己的同款：买入 ${history.bought_units} 件，${history.matched_sold_units} 件已配对，${history.unmatched_units} 件未配对；已售件持货中位数 ${holdingDays(history.completed_median_days)}。30 天内配对 ${cohortText(history.windows['30'])}。自己的近 7 / 30 天成交 ${history.own_sales_7d} / ${history.own_sales_30d} 件；最近成交 ${history.last_sale_at ? orderTime(history.last_sale_at) : '无'}。` +
    (history.unmatched_units ? ` 仍未配对的最长已持有 ${holdingDays(history.oldest_unmatched_days)}。` : '') +
    (history.stale ? ' 历史订单快照超过 1 小时，请同步后再判断。' : '');
}

function renderDemand(row, result) {
  const demand = result.liquidity;
  const cell = textCell(row, !demand || demand.status === 'unverified' ? '需求尚未核实' :
    demand.open_requests ? `${demand.requests_complete ? '' : '已看到至少 '}${demand.open_requests} 条有效求购` : '当前无公开求购');
  cell.className = 'market-demand';
  function note(text) {
    const small = document.createElement('small'); small.className = 'order-note'; small.textContent = text;
    cell.appendChild(small);
  }
  if (demand?.best_request_price != null && demand.request_profit != null) {
    note(`最高求购 ${money(demand.best_request_price)}`);
    const profit = document.createElement('strong');
    profit.className = Number(demand.request_profit) >= 0 ? 'flow-in' : 'flow-out';
    profit.textContent = `按求购利润 ${money(demand.request_profit)} / ${(Number(demand.request_roi) * 100).toFixed(2)}%`;
    cell.appendChild(profit);
    note(`求购价扣费预计到账 ${money(demand.request_pricing?.estimated_cash_receipt)}`);
  }
  if (demand?.ask_listings != null) {
    note(`${demand.ask_listings} 条在售 · ${demand.asks_complete ? '' : '采样至少 '}${demand.sampled_stock} 件库存`);
    if (demand.near_lowest_stock != null) note(`最低价 +5% 内至少 ${demand.near_lowest_stock} 件竞争库存`);
    const trend = demand.trend;
    note(trend?.status === 'observed' ? `${trend.hours} 小时：挂价 ${signed(trend.ask_change_pct, '%')} · 求购单 ${signed(trend.open_requests_change)}` : '价格和需求趋势：正在积累观察');
    const button = document.createElement('button'); button.type = 'button'; button.className = 'small-button ghost';
    button.textContent = '查看市场趋势'; button.addEventListener('click', () => openMarketTrend(result));
    cell.appendChild(button);
  }
  if (demand?.observed_at && Date.now() - Date.parse(demand.observed_at) > 300000) {
    note('此报价快照已超过 5 分钟；成交前请重新扫描');
  }
  note('全市场近 7/30 天销量、售出天数：未知');
  const own = result.own_history;
  if (own?.status === 'personal_history') {
    note(`自己的同款 ${own.bought_units} 件进货 · ${own.matched_sold_units} 件已配对；已售件持货中位数 ${holdingDays(own.completed_median_days)}`);
    note(`30 天内配对 ${cohortText(own.windows['30'])}`);
    note(`个人近 30 天成交 ${own.own_sales_30d} 件 · 最近 ${own.last_sale_at ? orderTime(own.last_sale_at) : '无成交'}`);
    if (own.matched_sold_units < 3 || own.own_sales_30d === 0) note('历史样本少或缺近期成交，不据此认定当前卖得快');
    if (own.unmatched_over_30d) note(`自己的 ${own.unmatched_over_30d} 件已持有超过 30 天仍未配对`);
  } else note('自己尚无此款进货样本，挂价差仍需观察');
  if (demand?.request_issue) note(demand.request_issue);
}

function drawMarketChart(samples) {
  const target = $('market-trend-chart'); target.replaceChildren();
  if (samples.length < 2) { target.textContent = '只有本轮观察，尚不能绘制趋势。巡航会继续积累。'; return; }
  const values = samples.flatMap(point => [point.lowest_ask, point.best_request_price]).filter(value => value != null).map(Number);
  if (!values.length) return;
  const lower = Math.min(...values), upper = Math.max(...values), span = upper - lower || 1;
  const times = samples.map(point => Date.parse(point.observed_at)), timeSpan = times.at(-1) - times[0] || 1;
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg'); svg.setAttribute('viewBox', '0 0 700 200');
  svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', '公开挂价与求购价的观察变化');
  for (const [field, color] of [['lowest_ask', '#50d0ad'], ['best_request_price', '#efc46a']]) {
    let prior = null;
    samples.forEach((sample, index) => {
      if (sample[field] == null) { prior = null; return; }
      const x = 40 + (times[index] - times[0]) / timeSpan * 620, y = 160 - (Number(sample[field]) - lower) / span * 125;
      if (prior) {
        const line = document.createElementNS(ns, 'line');
        for (const [key, value] of Object.entries({x1: prior.x, y1: prior.y, x2: x, y2: y, stroke: color, 'stroke-width': 2})) line.setAttribute(key, value);
        svg.appendChild(line);
      }
      const circle = document.createElementNS(ns, 'circle');
      for (const [key, value] of Object.entries({cx: x, cy: y, r: 4, fill: color})) circle.setAttribute(key, value);
      const title = document.createElementNS(ns, 'title'); title.textContent = `${orderTime(sample.observed_at)} · ${money(sample[field])}`;
      circle.appendChild(title); svg.appendChild(circle); prior = {x, y};
    });
  }
  for (const [value, y] of [[upper, 22], [lower, 180]]) {
    const label = document.createElementNS(ns, 'text'); label.setAttribute('x', '8'); label.setAttribute('y', y); label.setAttribute('fill', '#aabfc0'); label.textContent = money(value); svg.appendChild(label);
  }
  target.appendChild(svg);
}

function openMarketTrend(result) {
  const demand = result.liquidity, trend = demand?.trend;
  $('market-trend-title').textContent = `${result.market_title} · 市场需求与价格趋势`;
  const requestFees = demand?.request_pricing;
  $('market-trend-summary').textContent = (trend?.status === 'observed' ?
    `保留最近 7 天 ${trend.observations} 次观察；比较 ${orderTime(trend.from)} → ${orderTime(trend.to)}。挂价 ${signed(trend.ask_change_pct, '%')}；库存 ${signed(trend.stock_change, ' 件')}；有效求购 ${signed(trend.open_requests_change, ' 条')}。` :
    '首轮观察，至少间隔 30 分钟后再比较趋势。') + (requestFees ?
      ` 求购价费用：卖出费 ${money(requestFees.estimated_sell_fee)}，提现分摊 ${money(requestFees.estimated_payout_fee)}，单件利润 ${money(demand.request_profit)}。` : '');
  const samples = trend?.samples || [demand];
  drawMarketChart(samples);
  const body = $('market-trend-rows'); body.replaceChildren();
  for (const point of samples) {
    const row = document.createElement('tr');
    textCell(row, orderTime(point.observed_at)); textCell(row, money(point.lowest_ask)); textCell(row, money(point.best_request_price));
    textCell(row, point.asks_complete ? `${point.stock} 件` : '列表采样不完整');
    textCell(row, point.open_requests == null ? '未核实' : `${point.requests_complete ? '' : '至少 '}${point.open_requests} 条`);
    body.appendChild(row);
  }
  $('market-sales-evidence').textContent = `近 7 天销量：未知；近 30 天销量：未知；预计售出天数：未知。${demand.sales_evidence || '尚无可信成交时间记录'}。求购发布日期只统计仍然有效的求购单，不能代表实际销量。`;
  let personal = $('market-own-history');
  if (!personal) { personal = document.createElement('div'); personal.id = 'market-own-history'; personal.className = 'product-info'; $('market-sales-evidence').after(personal); }
  personal.replaceChildren();
  const heading = document.createElement('strong'); heading.textContent = '我们自己的销售证据'; personal.appendChild(heading);
  const ownText = document.createElement('p'); ownText.textContent = ownHistoryText(result.own_history); personal.appendChild(ownText);
  if (result.own_history?.observed_price_profit != null) {
    const history = result.own_history, price = document.createElement('p');
    price.textContent = `我们近 30 天 ${history.gross_price_samples_30d} 件普通成交的价格下四分位 ${money(history.observed_lower_quartile_gross_30d)}；若这次也按该价格卖出，扣当前卖出费及提现费的利润 ${money(history.observed_price_profit)}，收益率 ${(Number(history.observed_price_roi) * 100).toFixed(2)}%。这仍是历史价格情景，不能保证成交。`;
    personal.appendChild(price);
  }
  $('market-trend-dialog').showModal();
}

async function refreshResults() {
  const results = await api('/api/assessments');
  if (results.some(item => item.matching?.offer)) await refreshProductMappings();
  const state = $('result-filter').value;
  const rows = results.filter(result => state === 'all' || result.verdict === state ||
    (state === 'unavailable' && ['no_quote', 'error'].includes(result.verdict)));
  rows.sort((a, b) => {
    const rank = Number(b.verdict === 'opportunity') - Number(a.verdict === 'opportunity');
    return rank || (a.verdict === 'opportunity' ? Number(b.liquidity?.request_profit) - Number(a.liquidity?.request_profit) :
      a.verdict === 'price_only' && b.verdict === 'price_only' ? Number(b.net_profit) - Number(a.net_profit) : b.id - a.id);
  });
  $('result-count').textContent = `显示 ${rows.length} / ${results.length} 件 · 有求购候选按求购利润排序`;
  const body = $('results');
  body.replaceChildren();
  if (!rows.length) {
    const row = document.createElement('tr');
    const cell = textCell(row, results.length ? state === 'opportunity' ? '本轮没有求购价也达到利润门槛的商品；可查看“仅有挂价差”或“全部结果”。' : '此条件下没有商品；可选择“全部结果”查看' : '暂无扫描结果'); cell.colSpan = 8; cell.className = 'empty';
    body.appendChild(row); return;
  }
  for (const result of rows) {
    const row = document.createElement('tr');
    row.dataset.assessmentId = result.id;
    const name = document.createElement('td');
    name.appendChild(link(result.offer_url, result.offer_title)); row.appendChild(name);
    if (result.offer_alternate_titles?.length) {
      const alternate = document.createElement('small'); alternate.className = 'order-note';
      alternate.textContent = `官方名称：${result.offer_alternate_titles.join(' / ')}`;
      name.appendChild(alternate);
    }
    textCell(row, money(result.buy_price));
    const quote = document.createElement('td');
    if (result.market_title) quote.appendChild(link(result.market_url, result.market_title));
    else quote.textContent = '未确认对应商品';
    if (result.market_price != null) {
      const prices = document.createElement('small'); prices.className = 'order-note';
      prices.textContent = `最低在售挂价 ${money(result.market_price)} · 估算挂 ${money(result.target_sell_price)}`;
      quote.appendChild(prices);
    }
    const observed = document.createElement('small'); observed.className = 'order-note';
    observed.textContent = `报价采集：${orderTime(result.observed_at)}`;
    quote.appendChild(observed);
    row.appendChild(quote);
    renderDemand(row, result);
    const profit = textCell(row, money(result.net_profit));
    profit.className = result.net_profit == null ? '' : Number(result.net_profit) >= 0 ? 'flow-in' : 'flow-out';
    if (result.pricing && result.net_profit != null) {
      const pricing = result.pricing;
      const cash = document.createElement('small'); cash.className = 'order-note';
      cash.textContent = `按挂价卖出才有：到账 ${money(pricing.estimated_cash_receipt)}`;
      profit.appendChild(cash);
      const details = document.createElement('details'); details.className = 'candidate-fees';
      const summary = document.createElement('summary'); summary.textContent = '费用明细';
      details.appendChild(summary);
      for (const text of [
        `卖出费 ${(Number(pricing.sell_fee_rate) * 100).toFixed(2)}%：${money(pricing.estimated_sell_fee)}`,
        `钱包净入 ${money(pricing.estimated_wallet_credit)}`,
        `提现分摊 ${(Number(pricing.payout_fee_rate) * 100).toFixed(2)}%：${money(pricing.estimated_payout_fee)}`,
        `预计到账 ${money(pricing.estimated_cash_receipt)} − 进货 ${money(result.buy_price)}`,
        `门槛：利润 ≥ ${money(pricing.min_profit)} 且收益率 ≥ ${(Number(pricing.min_roi) * 100).toFixed(2)}%`,
        pricing.reused_quote === 'true' ? `沿用原报价，费率重算：${orderTime(pricing.calculated_at)}` : null
      ].filter(Boolean)) {
        const note = document.createElement('small'); note.className = 'order-note'; note.textContent = text;
        details.appendChild(note);
      }
      profit.appendChild(details);
    }
    textCell(row, result.roi == null ? '—' : `${(Number(result.roi) * 100).toFixed(2)}%`);
    const verdict = document.createElement('td');
    verdict.appendChild(tag(result.verdict, verdictLabels[result.verdict] || result.verdict));
    const reason = document.createElement('small'); reason.className = 'order-note'; reason.textContent = result.reason;
    verdict.appendChild(reason); row.appendChild(verdict);
    const evidence = document.createElement('details'); evidence.className = 'product-evidence';
    const summary = document.createElement('summary'); summary.textContent = '匹配依据'; evidence.appendChild(summary);
    const matching = result.matching;
    summary.addEventListener('click', event => { event.preventDefault(); openProductEvidence(result); });
    verdict.appendChild(evidence);
    const action = document.createElement('td');
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'small-button ghost'; button.textContent = '记录买入';
    button.addEventListener('click', () => openEntry({kind: 'purchase', result}));
    action.appendChild(button); row.appendChild(action);
    if (result.market_candidates?.some(item => item.identity) && matching?.offer) {
      const check = document.createElement('button'); check.type = 'button'; check.className = 'small-button ghost'; check.textContent = '核对对应商品';
      check.addEventListener('click', () => openProductEntry(result)); action.prepend(check);
    }
    body.appendChild(row);
  }
}

async function refreshTrades() {
  const [trades, cash] = await Promise.all([api('/api/trades'), api('/api/cash')]);
  $('manual-metrics').hidden = trades.length === 0;
  $('manual-ledger-help').textContent = trades.length ?
    `已登记 ${trades.length} 笔。这里只计算在下方逐笔创建并更新的交易；上方账号历史订单不在其中。` :
    '目前 0 笔。这里只统计在本系统逐笔登记的买入、售出与到账；上方自动读取的账号历史订单不在其中。';
  $('realized-profit').textContent = money(cash.realized_profit);
  $('capital-tied').textContent = money(cash.capital_tied);
  $('open-positions').textContent = cash.open_positions;
  $('awaiting-payout').textContent = cash.awaiting_payout;
  $('cash-received').textContent = money(cash.cash_received);
  const body = $('trades'); body.replaceChildren();
  if (!trades.length) {
    const row = document.createElement('tr');
    const cell = textCell(row, '尚未在本系统逐笔登记交易；上方已有订单统计仍可独立查看'); cell.colSpan = 6; cell.className = 'empty';
    body.appendChild(row); return;
  }
  for (const trade of trades) {
    const row = document.createElement('tr');
    const name = document.createElement('td'); name.appendChild(link(trade.offer_url, trade.title)); row.appendChild(name);
    const state = document.createElement('td'); state.appendChild(tag(trade.state, stateLabels[trade.state])); row.appendChild(state);
    textCell(row, money(trade.actual_cost));
    textCell(row, `${money(trade.ask_price)} / ${money(trade.sale_gross)}`);
    textCell(row, money(trade.payout));
    const action = document.createElement('td');
    if (actions[trade.state]) {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'small-button ghost'; button.textContent = '更新';
      button.addEventListener('click', () => openEntry({kind: 'transition', trade}));
      action.appendChild(button);
    }
    row.appendChild(action); body.appendChild(row);
  }
}

async function refreshAll() {
  await Promise.all([refreshResults(), refreshTrades()]);
}
function openEntry(value) {
  entry = value;
  const buying = value.kind === 'purchase';
  $('dialog-title').textContent = buying ? '记录实际买入' : '更新交易阶段';
  $('dialog-subtitle').textContent = buying ? value.result.offer_title : value.trade.title;
  $('event-label').hidden = buying;
  const selector = $('entry-event'); selector.replaceChildren();
  if (!buying) {
    for (const [event, label] of actions[value.trade.state]) {
      const option = document.createElement('option'); option.value = event; option.textContent = label;
      selector.appendChild(option);
    }
  }
  $('entry-amount').value = buying ? value.result.buy_price : '';
  $('entry-fee').value = '0'; $('entry-reference').value = '';
  updateFeeVisibility();
  $('entry-dialog').showModal();
}
function updateFeeVisibility() {
  $('fee-label').hidden = !entry || entry.kind === 'purchase' || $('entry-event').value !== 'sold';
}
async function saveEntry(event) {
  event.preventDefault();
  if (!entry) return;
  const amount = $('entry-amount').value;
  const reference = $('entry-reference').value.trim();
  try {
    if (entry.kind === 'purchase') {
      await api('/api/trades', {method: 'POST', body: JSON.stringify({assessment_id: entry.result.id, actual_cost: amount, reference})});
    } else {
      await api(`/api/trades/${entry.trade.id}/${$('entry-event').value}`, {
        method: 'POST', body: JSON.stringify({amount, fee: $('entry-fee').value || '0', reference})
      });
    }
    $('entry-dialog').close(); entry = null; notice('资金记录已保存。'); await refreshTrades();
  } catch (error) { notice(error.message); }
}

async function init() {
  try {
    token = (await api('/api/session')).token;
    const config = await api('/api/config');
    $('pages').max = config.max_pages;
    $('fee-note').textContent = `预估卖出费 ${(Number(config.sell_fee_rate) * 100).toFixed(2)}% · 提现费 ${(Number(config.payout_fee_rate) * 100).toFixed(2)}%（另扣）`;
    $('profit-rule').textContent = `利润怎么算：建议挂价 = 采样最低在售挂价 − ${money(config.undercut)}；预计到账 =（建议挂价 − 卖出手续费）÷（1 + 提现费率）；潜在套现利润 = 预计到账 − 进货成本。候选同时要求利润 ≥ ${money(config.min_profit ?? '0.50')}/件、成本收益率 ≥ ${(Number(config.min_roi ?? '0.05') * 100).toFixed(2)}%。费用及金额估算到分，提现费按比例分摊到单件，实际以成交和提现批次为准。`;
    $('clock').textContent = new Date().toLocaleDateString('zh-CN');
    await Promise.all([refreshStatus(), refreshAll(), refreshCruise(), refreshAccountOrders(), refreshPayouts()]);
    setInterval(() => refreshStatus().catch(error => notice(error.message)), 2000);
    setInterval(() => Promise.all([refreshAll(), refreshCruise(), refreshAccountOrders(), refreshPayouts()]).catch(error => notice(error.message)), 10000);
  } catch (error) { notice(error.message); }
}

$('scan-form').addEventListener('submit', async event => {
  event.preventDefault(); notice('');
  try {
    await api('/api/scans', {method: 'POST', body: JSON.stringify({keyword: $('keyword').value, pages: Number($('pages').value)})});
    await refreshStatus();
  } catch (error) { notice(error.message); }
});
$('cancel-scan').addEventListener('click', async () => {
  try { await api('/api/scans/cancel', {method: 'POST'}); await refreshStatus(); }
  catch (error) { notice(error.message); }
});
$('refresh').addEventListener('click', () => refreshAll().catch(error => notice(error.message)));
$('result-filter').addEventListener('change', () => refreshResults().catch(error => notice(error.message)));
$('market-trend-close').addEventListener('click', () => $('market-trend-dialog').close());
$('sync-orders').addEventListener('click', async () => {
  try {
    await api('/api/account-orders/sync', {method: 'POST'});
    await refreshAccountOrders();
  } catch (error) { $('account-order-state').textContent = error.message; }
});
$('sync-payouts').addEventListener('click', async () => {
  try {
    await api('/api/payouts/sync', {method: 'POST'});
    await refreshPayouts();
  } catch (error) { $('payout-state').textContent = error.message; }
});
$('bank-cancel').addEventListener('click', () => $('bank-dialog').close());
$('bank-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!bankBillId) return;
  try {
    await api(`/api/payouts/${encodeURIComponent(bankBillId)}/receipt`, {
      method: 'POST', body: JSON.stringify({amount: $('bank-amount').value,
        received_at: $('bank-date').value, note: $('bank-note').value.trim()})
    });
    $('bank-dialog').close(); bankBillId = null;
    notice('银行到账核对已保存。');
    await Promise.all([refreshPayouts(), refreshAccountOrders()]);
  } catch (error) { $('payout-state').textContent = error.message; }
});
$('account-ledger-form').addEventListener('submit', event => {
  event.preventDefault(); accountPage = 1;
  refreshAccountOrders().catch(error => $('ledger-count').textContent = error.message);
});
$('account-platform').addEventListener('change', () => {
  accountPage = 1; refreshAccountOrders().catch(error => $('ledger-count').textContent = error.message);
});
$('account-state').addEventListener('change', () => {
  accountPage = 1; refreshAccountOrders().catch(error => $('ledger-count').textContent = error.message);
});
$('inventory-form').addEventListener('submit', event => {
  event.preventDefault(); inventoryPage = 1;
  refreshAccountOrders().catch(error => $('inventory-count').textContent = error.message);
});
$('inventory-state').addEventListener('change', () => {
  inventoryPage = 1; refreshAccountOrders().catch(error => $('inventory-count').textContent = error.message);
});
$('inventory-prev').addEventListener('click', () => {
  if (inventoryPage > 1) inventoryPage -= 1;
  refreshAccountOrders().catch(error => $('inventory-count').textContent = error.message);
});
$('inventory-next').addEventListener('click', () => {
  inventoryPage += 1;
  refreshAccountOrders().catch(error => $('inventory-count').textContent = error.message);
});
$('unconfirmed-prev').addEventListener('click', () => {
  if (unconfirmedPage > 1) unconfirmedPage -= 1;
  refreshAccountOrders().catch(error => $('unconfirmed-count').textContent = error.message);
});
$('unconfirmed-next').addEventListener('click', () => {
  unconfirmedPage += 1;
  refreshAccountOrders().catch(error => $('unconfirmed-count').textContent = error.message);
});
$('match-state').addEventListener('change', () => renderMatches(matchSnapshot));
$('product-choice').addEventListener('change', updateProductChoice);
$('product-evidence-close').addEventListener('click', () => { $('product-evidence-dialog').close(); productEvidenceEntry = null; });
$('product-evidence-review').addEventListener('click', () => {
  const result = productEvidenceEntry; $('product-evidence-dialog').close(); productEvidenceEntry = null;
  if (result) openProductEntry(result);
});
$('product-cancel').addEventListener('click', () => { $('product-dialog').close(); productEntry = null; });
$('product-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!productEntry || !$('product-confirmed').checked) return;
  $('product-save').disabled = true; $('product-feedback').textContent = '正在保存对应关系…';
  try {
    await api('/api/product-mappings', {method: 'POST', body: JSON.stringify({
      assessment_id: productEntry.id, steampy_id: $('product-choice').value, note: $('product-note').value.trim()
    })});
    $('product-dialog').close(); productEntry = null;
    notice('商品对应关系已保存。下一轮扫描会核对商品信息并重新取价；可在“已确认的商品对应关系”中撤销。');
    await refreshResults();
  } catch (error) { $('product-feedback').textContent = error.message; $('product-save').disabled = false; }
});
$('ledger-prev').addEventListener('click', () => {
  if (accountPage > 1) accountPage -= 1;
  refreshAccountOrders().catch(error => $('ledger-count').textContent = error.message);
});
$('ledger-next').addEventListener('click', () => {
  accountPage += 1;
  refreshAccountOrders().catch(error => $('ledger-count').textContent = error.message);
});
$('toggle-cruise').addEventListener('click', async () => {
  try {
    await api(cruiseEnabled ? '/api/cruise/pause' : '/api/cruise/resume', {method: 'POST'});
    await Promise.all([refreshCruise(), refreshStatus()]);
  } catch (error) { notice(error.message); }
});
$('entry-event').addEventListener('change', updateFeeVisibility);
$('entry-form').addEventListener('submit', saveEntry);
$('dialog-cancel').addEventListener('click', () => $('entry-dialog').close());
init();

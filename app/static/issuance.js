/* 分批退货签发面板：逐批交代累计退款、运费与订单实付余额。 */
(function () {
  var currentOrder = null;
  function $(id) { return document.getElementById(id); }
  function esc(value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (ch) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[ch];
    });
  }
  function fen(value) {
    var n = Number(value || 0);
    return (n / 100).toFixed(2) + ' 元';
  }
  async function request(url, options) {
    var response = await fetch(url, Object.assign({headers: {'Content-Type': 'application/json'}}, options || {}));
    var data = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
    return data;
  }
  function parseQty(text) {
    var out = {};
    text.split(',').map(function (part) { return part.trim(); }).filter(Boolean).forEach(function (part) {
      var pair = part.split('=');
      if (pair.length !== 2) throw new Error('数量格式应为 行号=数量，例如 O1L1=1');
      var qty = parseInt(pair[1].trim(), 10);
      if (!Number.isInteger(qty) || qty < 0) throw new Error('退货数量必须是非负整数');
      out[pair[0].trim()] = qty;
    });
    return out;
  }
  function setNotice(text) {
    var node = $('notice');
    if (node) node.textContent = text || '';
  }
  function renderCumulative(status) {
    currentOrder = status;
    $('issuance-state').textContent = status.complete ? '已退齐 · 运费已退一次' : '退货进行中';
    var head =
      '<div class="cumulative-head">' +
      '<div class="ledger-row"><span>订单实付</span><strong>' + fen(status.payment) + '</strong></div>' +
      '<div class="ledger-row"><span>整单一次退清目标</span><strong>' + fen(status.full_target) + '</strong></div>' +
      '<div class="ledger-row"><span>已签发累计退款</span><strong class="check-ok">' + fen(status.cumulative_refund) + '</strong></div>' +
      '<div class="ledger-row"><span>实付余额（未退）</span><strong>' + fen(status.remaining) + '</strong></div>' +
      '<div class="ledger-row"><span>其中已退运费</span><strong>' + fen(status.freight_refunded) + '</strong></div>' +
      '<div class="ledger-row"><span>台账守恒（批次=分录）</span><strong class="' +
        (status.ledger_balanced ? 'check-ok' : 'check-bad') + '">' + (status.ledger_balanced ? '通过' : '异常') +
        ' · ' + status.batch_count + ' 批 / ' + status.ledger_count + ' 条分录</strong></div>' +
      '</div>';
    var rows = status.batches.length ? status.batches.map(function (batch) {
      var lines = (batch.detail && batch.detail.lines || []).map(function (line) {
        return esc(line.line_id) + ' ×' + line.qty + ' → ' + fen(line.refund);
      }).join('；');
      return '<tr>' +
        '<td class="num">第 ' + batch.seq + ' 批' + (batch.is_final ? '（退齐）' : '') + '</td>' +
        '<td class="num">' + fen(batch.amount) + '</td>' +
        '<td class="num">' + fen(batch.freight_refund) + '</td>' +
        '<td class="num">' + fen(batch.cumulative_after) + '</td>' +
        '<td>' + lines + '</td>' +
        '<td><small>' + esc((batch.detail && batch.detail.comment) || '') + '</small></td>' +
        '</tr>';
    }).join('') : '<tr><td colspan="6" class="empty">尚未签发任何批次</td></tr>';
    var table =
      '<table class="issuance-table"><thead><tr>' +
      '<th>批次</th><th>本批退款</th><th>本批运费</th><th>签发后累计</th><th>行明细</th><th>意见</th>' +
      '</tr></thead><tbody>' + rows + '</tbody></table>';
    $('issuance-cumulative').innerHTML = head + table;
  }
  async function loadStatus(orderId) {
    var status = await request('/api/issuance/orders/' + encodeURIComponent(orderId));
    renderCumulative(status);
    $('iss-preview').disabled = false;
    $('iss-issue').disabled = false;
    $('iss-refresh').disabled = false;
  }
  async function register() {
    try {
      setNotice('');
      var orderId = $('iss-order').value.trim();
      if (!orderId) throw new Error('请填写订单号');
      var profile = $('profile') ? $('profile').value : 'clean';
      await request('/api/issuance/orders', {method: 'POST', body: JSON.stringify({order_id: orderId, profile: profile})});
      await loadStatus(orderId);
    } catch (error) { setNotice(error.message); }
  }
  async function preview() {
    try {
      setNotice('');
      var orderId = $('iss-order').value.trim();
      var qty = parseQty($('iss-qty').value);
      var plan = await request('/api/issuance/orders/' + encodeURIComponent(orderId) + '/preview',
        {method: 'POST', body: JSON.stringify({returned_qty: qty})});
      var message = '预览：本批退 ' + fen(plan.batch_amount) +
        (plan.freight_refund ? '（含运费 ' + fen(plan.freight_refund) + '）' : '（退齐前不退运费）') +
        '；签发后累计 ' + fen(plan.cumulative_after) + ' / 实付 ' + fen(plan.payment) +
        '，余 ' + fen(plan.remaining_after) + (plan.complete ? '；本批退齐并对齐整单目标。' : '。');
      setNotice(message);
    } catch (error) { setNotice(error.message); }
  }
  async function issue() {
    try {
      setNotice('');
      var orderId = $('iss-order').value.trim();
      var qty = parseQty($('iss-qty').value);
      var key = $('iss-key').value.trim();
      if (!key) throw new Error('请填写请求键；重试时保持同一请求键即可幂等，不重复记账');
      var batch = await request('/api/issuance/orders/' + encodeURIComponent(orderId) + '/batches',
        {method: 'POST', body: JSON.stringify({returned_qty: qty, request_key: key, comment: $('comment') ? $('comment').value : ''})});
      await loadStatus(orderId);
      setNotice((batch.idempotent_hit ? '幂等命中，未重复记账：' : '已签发第 ' + batch.seq + ' 批：') +
        '本批 ' + fen(batch.amount) + '，签发后累计 ' + fen(batch.cumulative_after) +
        '，实付余额 ' + fen(currentOrder.remaining) + '。');
    } catch (error) { setNotice(error.message); }
  }
  function boot() {
    $('iss-register').addEventListener('click', register);
    $('iss-preview').addEventListener('click', preview);
    $('iss-issue').addEventListener('click', issue);
    $('iss-refresh').addEventListener('click', function () {
      var orderId = $('iss-order').value.trim();
      if (orderId) loadStatus(orderId).catch(function (error) { setNotice(error.message); });
    });
  }
  document.addEventListener('DOMContentLoaded', boot);
})();

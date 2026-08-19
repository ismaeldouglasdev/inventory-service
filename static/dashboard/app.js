// ===== OSPOS Dashboard PWA =====
// Real-time sales KPIs, stock alerts, goals with WebSocket + offline caching

(function () {
  'use strict';

  // ── Config ──────────────────────────────────────────────────────────────
  const API_BASE = '/v1/dashboard';
  const WS_URL = `ws${location.protocol === 'https:' ? 's' : ''}://${location.host}${API_BASE}/ws`;
  const CACHE_NAME = 'ospos-dashboard-v1';
  const CACHE_TTL = 5 * 60 * 1000; // 5 min

  // ── State ───────────────────────────────────────────────────────────────
  let ws = null;
  let reconnectTimer = null;
  let reconnectAttempts = 0;
  let hourlyChart = null;
  let currentPeriod = 'today';
  let customStart = '';
  let customEnd = '';
  let lastData = null;

  // ── DOM Elements ────────────────────────────────────────────────────────
  const els = {
    periodSelect: document.getElementById('periodSelect'),
    customRange: document.getElementById('customRange'),
    startDate: document.getElementById('startDate'),
    endDate: document.getElementById('endDate'),
    refreshBtn: document.getElementById('refreshBtn'),
    connStatus: document.getElementById('connStatus'),
    connOffline: document.getElementById('connOffline'),
    alertBadge: document.getElementById('alertBadge'),
    alertCard: document.getElementById('alertCard'),
    alertsSection: document.getElementById('alertsSection'),
    closeAlerts: document.getElementById('closeAlerts'),
    salesTotal: document.getElementById('salesTotal'),
    targetPct: document.getElementById('targetPct'),
    transactions: document.getElementById('transactions'),
    avgTicket: document.getElementById('avgTicket'),
    itemsSold: document.getElementById('itemsSold'),
    pendingReceivables: document.getElementById('pendingReceivables'),
    alertCount: document.getElementById('alertCount'),
    topPeriod: document.getElementById('topPeriod'),
    topItemsList: document.getElementById('topItemsList'),
    alertsList: document.getElementById('alertsList'),
    lastUpdate: document.getElementById('lastUpdate'),
    wsStatus: document.getElementById('wsStatus'),
    footer: document.getElementById('footer'),
  };

  // ── Formatters ──────────────────────────────────────────────────────────
  const fmtMoney = (v) => {
    if (v === null || v === undefined) return 'R$ 0,00';
    return new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL', minimumFractionDigits: 2 }).format(v);
  };
  const fmtNumber = (v) => new Intl.NumberFormat('pt-BR').format(v);
  const fmtTime = (iso) => {
    const d = new Date(iso);
    return d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
  };
  const fmtDate = (iso) => {
    const d = new Date(iso);
    return d.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit' });
  };

  // ── Chart (simple canvas bars) ──────────────────────────────────────────
  function drawHourlyChart(hourly, max) {
    const canvas = document.getElementById('hourlyChart');
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.scale(dpr, dpr);
    const w = rect.width;
    const h = rect.height;
    const padding = { top: 8, right: 8, bottom: 24, left: 8 };
    const cw = w - padding.left - padding.right;
    const ch = h - padding.top - padding.bottom;
    const barW = cw / 24 * 0.7;
    const gap = cw / 24 - barW;
    const maxVal = max || Math.max(...hourly, 1);

    // Clear
    ctx.clearRect(0, 0, w, h);

    // Grid lines (subtle)
    ctx.strokeStyle = 'rgba(138, 147, 166, 0.1)';
    ctx.lineWidth = 1;
    for (let i = 1; i <= 4; i++) {
      const y = padding.top + (ch / 4) * i;
      ctx.beginPath();
      ctx.moveTo(padding.left, y);
      ctx.lineTo(w - padding.right, y);
      ctx.stroke();
    }

    // Bars
    hourly.forEach((val, i) => {
      const x = padding.left + i * (barW + gap) + gap / 2;
      const bh = val / maxVal * ch;
      const y = padding.top + ch - bh;

      // Gradient
      const grad = ctx.createLinearGradient(0, padding.top + ch, 0, padding.top);
      grad.addColorStop(0, 'rgba(46, 204, 113, 0.3)');
      grad.addColorStop(1, 'rgba(46, 204, 113, 0.9)');

      ctx.fillStyle = grad;
      ctx.fillRect(x, y, barW, bh);

      // Hour label
      ctx.fillStyle = 'rgba(138, 147, 166, 0.6)';
      ctx.font = '10px var(--mono)';
      ctx.textAlign = 'center';
      ctx.fillText(i.toString().padStart(2, '0'), x + barW / 2, h - padding.bottom + 14);

      // Value on top (if significant)
      if (val > maxVal * 0.05) {
        ctx.fillStyle = '#2ecc71';
        ctx.font = 'bold 10px var(--mono)';
        ctx.fillText(fmtMoney(val).replace('R$ ', ''), x + barW / 2, y - 4);
      }
    });

    // Y-axis label (max)
    ctx.fillStyle = 'rgba(138, 147, 166, 0.5)';
    ctx.font = '10px var(--mono)';
    ctx.textAlign = 'right';
    ctx.fillText(fmtMoney(maxVal), padding.left - 4, padding.top + 10);
  }

  // ── Render Functions ────────────────────────────────────────────────────
  function renderSummary(data) {
    els.salesTotal.textContent = fmtMoney(data.sales_total);
    els.transactions.textContent = fmtNumber(data.transactions);
    els.itemsSold.textContent = fmtNumber(data.items_sold);
    els.avgTicket.textContent = `Ticket médio: ${fmtMoney(data.avg_ticket)}`;
    els.pendingReceivables.textContent = `A receber: ${fmtMoney(data.pending_receivables)}`;
    els.targetPct.textContent = `Meta: ${data.target_pct}%`;
    els.targetPct.style.color = data.target_pct >= 100 ? '#2ecc71' : data.target_pct >= 75 ? '#f39c12' : '#e74c3c';

    // Hourly chart
    drawHourlyChart(data.hourly_sales || [], data.hourly_max || 0);

    // Top items
    const periodLabels = { today: 'hoje', yesterday: 'ontem', week: 'esta semana', month: 'este mês', custom: 'período' };
    els.topPeriod.textContent = periodLabels[data.period] || data.period;
    renderTopItems(data.top_items || []);

    // Update footer
    els.lastUpdate.textContent = `Atualizado: ${fmtTime(new Date().toISOString())}`;
  }

  function renderTopItems(items) {
    if (!items.length) {
      els.topItemsList.innerHTML = '<li class="item-loading">Nenhuma venda no período</li>';
      return;
    }
    els.topItemsList.innerHTML = items.map((item, i) => {
      const rankClass = i === 0 ? 'gold' : i === 1 ? 'silver' : i === 2 ? 'bronze' : '';
      return `
        <li>
          <span class="item-rank ${rankClass}">${i + 1}</span>
          <div class="item-info">
            <div class="item-name">${escapeHtml(item.name)}</div>
            <div class="item-meta">
              <span>${fmtNumber(item.qty)} un</span>
              <span>${fmtMoney(item.revenue)}</span>
            </div>
          </div>
          <span class="item-value">${fmtMoney(item.revenue)}</span>
        </li>
      `;
    }).join('');
  }

  function renderAlerts(alerts) {
    const count = alerts.length;
    els.alertBadge.textContent = count > 99 ? '99+' : count;
    els.alertBadge.hidden = count === 0;
    els.alertCard.hidden = count === 0;
    els.alertCount.textContent = count;

    if (!count) {
      els.alertsList.innerHTML = '<li class="item-loading">Nenhum alerta de estoque ✓</li>';
      return;
    }
    els.alertsList.innerHTML = alerts.map(a => {
      const isZero = a.stock_status === 1;
      const statusClass = isZero ? '' : 'irregular';
      const statusLabel = isZero ? 'ZERADO' : 'IRREGULAR';
      const qtyClass = isZero ? '' : 'irregular';
      return `
        <li>
          <span class="alert-status ${statusClass}" title="${statusLabel}"></span>
          <div class="alert-info">
            <div class="alert-name">${escapeHtml(a.name)}</div>
            <div class="alert-detail">${escapeHtml(a.item_number)} • Ponto: ${a.reorder_level}</div>
          </div>
          <span class="alert-qty ${qtyClass}">${fmtNumber(a.quantity)}</span>
        </li>
      `;
    }).join('');
  }

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, c => ({ '&': '&', '<': '<', '>': '>', '"': '"', "'": ''' }[c]));
  }

  // ── API Calls ───────────────────────────────────────────────────────────
  async function fetchSummary(period, start, end) {
    const params = new URLSearchParams({ period });
    if (period === 'custom' && start) {
      params.set('start', start);
      if (end) params.set('end', end);
    }
    const res = await fetch(`${API_BASE}/summary?${params}`, { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  async function fetchAlerts() {
    const res = await fetch(`${API_BASE}/alerts?limit=20`, { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  async function loadData(showLoading = true) {
    if (showLoading) setLoading(true);
    try {
      const [summary, alerts] = await Promise.all([
        fetchSummary(currentPeriod, customStart, customEnd),
        fetchAlerts(),
      ]);
      lastData = { summary, alerts, ts: Date.now() };
      // Cache in localStorage for offline
      localStorage.setItem('dashboard_cache', JSON.stringify(lastData));
      renderSummary(summary);
      renderAlerts(alerts.alerts || []);
      showOffline(false);
    } catch (e) {
      console.error('Load failed:', e);
      showOffline(true);
      loadFromCache();
    } finally {
      if (showLoading) setLoading(false);
    }
  }

  function loadFromCache() {
    const cached = localStorage.getItem('dashboard_cache');
    if (cached) {
      try {
        const data = JSON.parse(cached);
        if (Date.now() - data.ts < CACHE_TTL) {
          renderSummary(data.summary);
          renderAlerts(data.alerts.alerts || []);
          els.lastUpdate.textContent += ' (cache)';
        }
      } catch (e) { console.error('Cache parse error:', e); }
    }
  }

  function showOffline(offline) {
    els.connStatus.hidden = offline;
    els.connOffline.hidden = !offline;
  }

  function setLoading(loading) {
    els.refreshBtn.style.opacity = loading ? '0.5' : '1';
    els.refreshBtn.disabled = loading;
  }

  // ── WebSocket ───────────────────────────────────────────────────────────
  function connectWS() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

    ws = new WebSocket(WS_URL);
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
      console.log('[WS] Connected');
      reconnectAttempts = 0;
      els.wsStatus.classList.add('ws-connected');
      els.wsStatus.classList.remove('ws-disconnected');
      els.wsStatus.title = 'WebSocket conectado';
      showOffline(false);
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        handleWSMessage(msg);
      } catch (e) { console.error('[WS] Parse error:', e); }
    };

    ws.onclose = () => {
      console.log('[WS] Disconnected');
      els.wsStatus.classList.remove('ws-connected');
      els.wsStatus.classList.add('ws-disconnected');
      els.wsStatus.title = 'WebSocket desconectado';
      scheduleReconnect();
    };

    ws.onerror = (err) => {
      console.error('[WS] Error:', err);
    };
  }

  function handleWSMessage(msg) {
    if (msg.type === 'init') {
      // Initial state on connect
      if (msg.summary) {
        renderSummary(msg.summary);
      }
      if (msg.alerts) {
        renderAlerts(msg.alerts);
      }
      els.lastUpdate.textContent = `Atualizado: ${fmtTime(msg.ts)} (tempo real)`;
    } else if (msg.type === 'sale') {
      // Incremental sale update
      if (lastData && lastData.summary) {
        lastData.summary.transactions = msg.transactions;
        lastData.summary.sales_total = msg.sales_total;
        lastData.summary.avg_ticket = msg.avg_ticket;
        renderSummary(lastData.summary);
      }
      els.lastUpdate.textContent = `Atualizado: ${fmtTime(msg.ts)} (tempo real)`;
    } else if (msg.type === 'stock_alert') {
      // Stock alert change - refetch alerts
      fetchAlerts().then(res => {
        renderAlerts(res.alerts || []);
        if (lastData) lastData.alerts = res;
      }).catch(console.error);
      els.lastUpdate.textContent = `Atualizado: ${fmtTime(msg.ts)} (tempo real)`;
    }
  }

  function scheduleReconnect() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    const delay = Math.min(1000 * Math.pow(2, reconnectAttempts), 30000) + Math.random() * 1000;
    reconnectAttempts++;
    reconnectTimer = setTimeout(connectWS, delay);
  }

  function sendPing() {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send('ping');
    }
  }

  // ── Event Listeners ─────────────────────────────────────────────────────
  els.periodSelect.addEventListener('change', () => {
    currentPeriod = els.periodSelect.value;
    els.customRange.hidden = currentPeriod !== 'custom';
    if (currentPeriod === 'custom') {
      customStart = els.startDate.value || new Date().toISOString().split('T')[0];
      customEnd = els.endDate.value || customStart;
    }
    loadData();
  });

  els.startDate.addEventListener('change', () => {
    customStart = els.startDate.value;
    if (!customEnd || customEnd < customStart) customEnd = customStart;
    els.endDate.value = customEnd;
    loadData();
  });

  els.endDate.addEventListener('change', () => {
    customEnd = els.endDate.value;
    if (customStart > customEnd) customStart = customEnd;
    els.startDate.value = customStart;
    loadData();
  });

  els.refreshBtn.addEventListener('click', () => loadData(true));

  els.closeAlerts.addEventListener('click', () => {
    els.alertsSection.hidden = true;
  });

  els.alertCard.addEventListener('click', () => {
    els.alertsSection.hidden = false;
  });

  // Set default dates for custom range
  const today = new Date().toISOString().split('T')[0];
  els.startDate.value = today;
  els.endDate.value = today;
  els.startDate.max = today;
  els.endDate.max = today;

  // Periodic ping to keep WS alive
  setInterval(sendPing, 25000);

  // Visibility change - reconnect when tab becomes visible
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && (!ws || ws.readyState !== WebSocket.OPEN)) {
      connectWS();
    }
  });

  // ── Init ────────────────────────────────────────────────────────────────
  function init() {
    // Load cached data immediately
    loadFromCache();
    // Fetch fresh data
    loadData(true);
    // Connect WebSocket
    connectWS();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
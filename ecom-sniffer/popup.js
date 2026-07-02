const DASHBOARD = 'http://13.140.171.246:8000/dashboard';

function api(method, path, body) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: 'API_REQUEST', method, path, body }, (res) => {
      resolve(res || { ok: false, error: 'No response from background' });
    });
  });
}

function showError(msg) {
  const el = document.getElementById('error-banner');
  el.style.display = 'block';
  el.textContent = msg;
}

async function load() {
  const [summaryRes, pendingRes, oppsRes] = await Promise.allSettled([
    api('GET', '/log/summary'),
    api('GET', '/orders/pending'),
    api('GET', '/margin/opportunities'),
  ]);

  let anyError = false;

  // Stats from /log/summary
  if (summaryRes.status === 'fulfilled' && summaryRes.value?.ok) {
    const { events = {}, totals = {} } = summaryRes.value.data;
    document.getElementById('stat-listings').textContent = totals.active_listings ?? 0;
    document.getElementById('stat-sales').textContent = events.sale ?? 0;
    const sales = events.sale ?? 0;
    const errors = events.fulfillment_error ?? 0;
    const rate = (sales + errors) > 0
      ? Math.round(sales / (sales + errors) * 100) + '%'
      : 'N/A';
    document.getElementById('stat-rate').textContent = rate;
  } else {
    anyError = true;
  }

  // Pending orders count
  if (pendingRes.status === 'fulfilled' && pendingRes.value?.ok) {
    document.getElementById('pending-count').textContent =
      pendingRes.value.data.total_pending ?? 0;
  } else {
    document.getElementById('pending-count').textContent = '?';
    anyError = true;
  }

  // Top 3 opportunities
  if (oppsRes.status === 'fulfilled' && oppsRes.value?.ok) {
    const top3 = (oppsRes.value.data.opportunities || []).slice(0, 3);
    const tbody = document.getElementById('opps-tbody');
    tbody.innerHTML = '';
    if (top3.length === 0) {
      tbody.innerHTML = '<tr><td colspan="3" style="color:#8b949e;padding-top:4px;">No opportunities yet</td></tr>';
    } else {
      top3.forEach((o) => {
        const short = o.title.length > 30 ? o.title.slice(0, 30) + '…' : o.title;
        const tr = document.createElement('tr');
        tr.innerHTML = `<td title="${o.title.replace(/"/g, '&quot;')}">${short}</td>` +
          `<td>£${(o.competitor_price ?? 0).toFixed(2)}</td>` +
          `<td class="profit">+£${(o.target_profit ?? 0).toFixed(2)}</td>`;
        tbody.appendChild(tr);
      });
    }
  } else {
    document.getElementById('opps-tbody').innerHTML =
      '<tr><td colspan="3" style="color:#8b949e;">Could not load</td></tr>';
    anyError = true;
  }

  if (anyError) {
    showError('Could not reach API at 13.140.171.246:8000 — check the server is running.');
  }
}

document.addEventListener('DOMContentLoaded', () => {
  load();

  document.getElementById('btn-scan').addEventListener('click', async () => {
    const seller = prompt('Enter eBay seller username to scan:');
    if (!seller || !seller.trim()) return;

    const btn = document.getElementById('btn-scan');
    btn.disabled = true;
    btn.textContent = 'Scanning…';

    const res = await api('POST', '/competitors/scan', {
      seller_usernames: [seller.trim()],
      marketplace: 'EBAY_GB',
    });

    btn.disabled = false;
    btn.textContent = 'Scan Competitors';

    const resultEl = document.getElementById('scan-result');
    resultEl.style.display = 'block';
    if (res?.ok) {
      resultEl.style.color = '#3fb950';
      resultEl.textContent =
        `✓ ${res.data.total_listings} listings indexed for "${seller.trim()}" (${res.data.cached ? 'cached' : 'live'})`;
    } else {
      resultEl.style.color = '#f85149';
      resultEl.textContent = '✗ Scan failed: ' + (res?.error || res?.data?.message || 'Unknown error');
    }
  });

  document.getElementById('btn-opps').addEventListener('click', () => {
    chrome.tabs.create({ url: DASHBOARD });
  });

  document.getElementById('btn-orders').addEventListener('click', () => {
    chrome.tabs.create({ url: DASHBOARD });
  });

  document.getElementById('btn-set-key').addEventListener('click', async () => {
    const { apiKey: current } = await chrome.storage.local.get('apiKey');
    const next = prompt('Enter API key (sent as X-API-Key header):', current || '');
    if (next === null) return;
    if (next.trim()) {
      await chrome.storage.local.set({ apiKey: next.trim() });
    } else {
      await chrome.storage.local.remove('apiKey');
    }
    load();
  });
});

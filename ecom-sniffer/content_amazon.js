(function () {
  'use strict';

  if (document.getElementById('ecom-sniffer-host')) return;

  function extractAsin() {
    const m = location.pathname.match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/);
    return m ? m[1] : null;
  }

  function extractPrice() {
    const selectors = [
      '.priceToPay .a-offscreen',
      '#corePrice_feature_div .a-price .a-offscreen',
      '.apexPriceToPay .a-offscreen',
      '#priceblock_ourprice',
      '#priceblock_dealprice',
      '#price_inside_buybox',
      '.a-price.aok-align-center .a-offscreen',
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) {
        const n = parseFloat(el.textContent.replace(/[^0-9.]/g, ''));
        if (n > 0) return n;
      }
    }
    return null;
  }

  function extractTitle() {
    const el = document.getElementById('productTitle');
    return el ? el.textContent.trim() : document.title.split(':')[0].trim();
  }

  function extractImage() {
    const el =
      document.getElementById('landingImage') ||
      document.querySelector('#imgTagWrapperId img') ||
      document.querySelector('#main-image-container img');
    return el ? (el.getAttribute('data-old-hires') || el.src || '') : '';
  }

  function apiRequest(method, path, body) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage(
        { type: 'API_REQUEST', method, path, body },
        (res) => resolve(res || { ok: false, error: 'no response' })
      );
    });
  }

  function buildBadge() {
    const host = document.createElement('div');
    host.id = 'ecom-sniffer-host';
    host.style.cssText =
      'position:fixed;bottom:20px;right:20px;z-index:2147483647;font-family:system-ui,sans-serif;';
    document.body.appendChild(host);

    const shadow = host.attachShadow({ mode: 'open' });

    shadow.innerHTML = `
      <style>
        .badge {
          background:#0d1117;border:1px solid #30363d;border-radius:12px;
          padding:14px 16px;min-width:230px;
          box-shadow:0 8px 24px rgba(0,0,0,.65);color:#e6edf3;
        }
        .hdr {
          display:flex;align-items:center;gap:8px;
          margin-bottom:10px;font-weight:700;font-size:13px;color:#58a6ff;
        }
        .dot {
          width:8px;height:8px;border-radius:50%;
          background:#d29922;flex-shrink:0;
          animation:pulse 1s infinite;
        }
        .dot.ok  { background:#3fb950;animation:none; }
        .dot.err { background:#f85149;animation:none; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }
        .row {
          display:flex;justify-content:space-between;
          font-size:12px;padding:3px 0;color:#8b949e;
        }
        .row span+span { color:#e6edf3;font-weight:600; }
        .yes { color:#3fb950!important; }
        .no  { color:#f85149!important; }
        .btn-list {
          margin-top:12px;width:100%;padding:7px 0;
          background:#238636;color:#fff;border:none;border-radius:6px;
          font-size:12px;font-weight:600;cursor:pointer;
        }
        .btn-list:hover:not(:disabled) { background:#2ea043; }
        .btn-list:disabled { background:#21262d;color:#484f58;cursor:default; }
        .close {
          margin-left:auto;background:none;border:none;
          color:#8b949e;cursor:pointer;font-size:14px;padding:0;line-height:1;
        }
        .close:hover { color:#e6edf3; }
        .msg { font-size:11px;margin-top:6px;text-align:center;color:#8b949e;min-height:14px; }
      </style>
      <div class="badge">
        <div class="hdr">
          <span class="dot" id="dot"></span>
          <span>Ecom Sniffer</span>
          <button class="close" id="close-btn">✕</button>
        </div>
        <div class="row"><span>Amazon price</span><span id="v-price">—</span></div>
        <div class="row"><span>eBay min price</span><span id="v-list">—</span></div>
        <div class="row"><span>Target profit</span><span id="v-profit">—</span></div>
        <div class="row"><span>Viable</span><span id="v-viable">checking…</span></div>
        <button class="btn-list" id="list-btn" disabled>List on eBay</button>
        <div class="msg" id="msg"></div>
      </div>
    `;

    shadow.getElementById('close-btn').addEventListener('click', () => host.remove());

    return shadow;
  }

  async function run() {
    const asin = extractAsin();
    if (!asin) return;

    let price = extractPrice();
    if (!price) {
      await new Promise((r) => setTimeout(r, 2500));
      price = extractPrice();
    }
    if (!price) return;

    const shadow = buildBadge();

    shadow.getElementById('v-price').textContent = '$' + price.toFixed(2);

    const res = await apiRequest('POST', '/margin/calculate', { amazon_price: price });

    const dot = shadow.getElementById('dot');

    if (!res?.ok || !res.data) {
      dot.className = 'dot err';
      shadow.getElementById('v-viable').textContent = 'API error';
      shadow.getElementById('v-viable').className = 'no';
      return;
    }

    const d = res.data;
    dot.className = d.viable ? 'dot ok' : 'dot err';
    shadow.getElementById('v-list').textContent = '$' + d.minimum_list_price.toFixed(2);
    shadow.getElementById('v-profit').textContent = '+$' + d.target_profit.toFixed(2);

    const viableEl = shadow.getElementById('v-viable');
    viableEl.textContent = d.viable ? 'Yes ✓' : 'No ✗';
    viableEl.className = d.viable ? 'yes' : 'no';

    if (!d.viable) return;

    const listBtn = shadow.getElementById('list-btn');
    listBtn.disabled = false;

    listBtn.addEventListener('click', async () => {
      listBtn.disabled = true;
      listBtn.textContent = 'Listing…';

      const createRes = await apiRequest('POST', '/listings/create', {
        title: extractTitle(),
        amazon_price: price,
        amazon_asin: asin,
        ebay_list_price: d.minimum_list_price,
        image_url: extractImage(),
        condition: 'NEW',
      });

      if (createRes?.ok) {
        listBtn.textContent = 'Listed ✓';
        shadow.getElementById('msg').textContent =
          'ID: ' + (createRes.data.listing_id || 'created');
      } else {
        listBtn.disabled = false;
        listBtn.textContent = 'Retry';
        shadow.getElementById('msg').textContent =
          createRes?.data?.detail?.message || createRes?.error || 'Listing failed';
      }
    });
  }

  if (document.readyState === 'complete') {
    run();
  } else {
    window.addEventListener('load', run);
  }
})();

(function () {
  'use strict';

  if (document.getElementById('ecom-sniffer-ebay')) return;

  function extractSeller() {
    const m = location.pathname.match(/\/(?:str|usr)\/([^/?#]+)/);
    return m ? decodeURIComponent(m[1]) : null;
  }

  function apiRequest(method, path, body) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage(
        { type: 'API_REQUEST', method, path, body },
        (res) => resolve(res || { ok: false, error: 'no response' })
      );
    });
  }

  function inject(seller) {
    const host = document.createElement('div');
    host.id = 'ecom-sniffer-ebay';
    host.style.cssText =
      'position:fixed;top:70px;right:16px;z-index:2147483647;font-family:system-ui,sans-serif;';
    document.body.appendChild(host);

    const shadow = host.attachShadow({ mode: 'open' });

    shadow.innerHTML = `
      <style>
        .badge {
          background:#0d1117;border:1px solid #30363d;border-radius:10px;
          padding:12px 14px;min-width:210px;
          box-shadow:0 8px 24px rgba(0,0,0,.5);color:#e6edf3;
        }
        .hdr {
          display:flex;align-items:center;gap:8px;
          font-weight:700;font-size:13px;color:#58a6ff;margin-bottom:8px;
        }
        .sub { font-size:11px;color:#8b949e;margin-bottom:10px; }
        .close {
          margin-left:auto;background:none;border:none;
          color:#8b949e;cursor:pointer;font-size:14px;padding:0;line-height:1;
        }
        .close:hover { color:#e6edf3; }
        .scan-btn {
          width:100%;padding:8px 0;background:#1f6feb;color:#fff;
          border:none;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;
        }
        .scan-btn:hover:not(:disabled) { background:#388bfd; }
        .scan-btn:disabled { background:#21262d;color:#484f58;cursor:default; }
        .result { font-size:11px;margin-top:8px;text-align:center;min-height:14px;color:#8b949e; }
        .result.ok  { color:#3fb950; }
        .result.err { color:#f85149; }
      </style>
      <div class="badge">
        <div class="hdr">
          <span>🔍</span>
          <span>Ecom Sniffer</span>
          <button class="close" id="close-btn">✕</button>
        </div>
        <div class="sub" id="seller-label"></div>
        <button class="scan-btn" id="scan-btn">Scan with Ecom Sniffer</button>
        <div class="result" id="result"></div>
      </div>
    `;

    shadow.getElementById('seller-label').textContent = 'Seller: ' + seller;
    shadow.getElementById('close-btn').addEventListener('click', () => host.remove());

    shadow.getElementById('scan-btn').addEventListener('click', async () => {
      const btn = shadow.getElementById('scan-btn');
      const result = shadow.getElementById('result');
      btn.disabled = true;
      btn.textContent = 'Scanning…';
      result.textContent = '';
      result.className = 'result';

      const query = window.prompt(
        "What kind of product are you sourcing from this seller? (eBay's Browse API requires a keyword — a blank/too-generic search is rejected)"
      );
      if (!query) {
        btn.disabled = false;
        btn.textContent = 'Scan with Ecom Sniffer';
        result.className = 'result err';
        result.textContent = '✗ Scan needs a search keyword';
        return;
      }

      const res = await apiRequest('POST', '/competitors/scan', {
        seller_username: seller,
        query,
      });

      btn.disabled = false;
      btn.textContent = 'Scan with Ecom Sniffer';

      if (res?.ok) {
        const count = res.data.total_listings ?? 0;
        const src = res.data.cached ? 'cached' : 'live';
        result.className = 'result ok';
        result.textContent = `✓ ${count} listings indexed (${src})`;
      } else {
        result.className = 'result err';
        result.textContent = '✗ ' + (res?.error || res?.data?.message || 'Scan failed');
      }
    });
  }

  function init() {
    const seller = extractSeller();
    if (seller) inject(seller);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

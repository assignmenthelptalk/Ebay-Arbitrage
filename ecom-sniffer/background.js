const API_BASE = 'http://13.140.171.246:8000';

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type !== 'API_REQUEST') return false;

  const { method = 'GET', path, body } = msg;
  const url = `${API_BASE}${path}`;

  chrome.storage.local.get('apiKey').then(({ apiKey }) => {
    const headers = body ? { 'Content-Type': 'application/json' } : {};
    if (apiKey) headers['X-API-Key'] = apiKey;

    fetch(url, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    })
      .then(async (r) => {
        const data = await r.json().catch(() => ({}));
        sendResponse({ ok: r.ok, status: r.status, data });
      })
      .catch((err) => {
        sendResponse({ ok: false, error: err.message, data: null });
      });
  });

  return true; // keep message channel open for async response
});

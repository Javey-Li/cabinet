(function () {
  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (character) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character];
    });
  }
  function safeAsset(value) {
    const path=String(value == null ? '' : value);
    if(!/^[A-Za-z0-9._/-]+$/.test(path) || path.startsWith('/') || path.split('/').includes('..')) return '';
    return path;
  }
  function safeColor(value) {
    const color=String(value == null ? '' : value);
    return /^#[0-9a-fA-F]{3,8}$/.test(color) ? color : '';
  }
  window.cabinetEscapeHtml=escapeHtml;
  window.cabinetSafeAsset=safeAsset;
  window.cabinetSafeColor=safeColor;
  window.cabinetSafeUrl=function (value) {
    const url=String(value == null ? '' : value).trim();
    return /^https?:\/\//i.test(url) ? url : '';
  };
  window.cabinetState={
    async read() {
      if (location.protocol === 'file:') return {trashed:[],later:[]};
      try {
        const response = await fetch('/api/state', {cache:'no-store'});
        if (!response.ok) return {trashed:[],later:[]};
        return await response.json();
      } catch (error) {
        return {trashed:[],later:[]};
      }
    },
    async update(action, id) {
      if (location.protocol === 'file:') throw new Error('请先运行 python3 drop.py start');
      const response = await fetch('/drop', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({kind:'state',action,id})
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || '状态没存上');
      return payload;
    }
  };
  const seen = new Set();
  const fingerprints = new Set();
  function merge() {
    const entries = (window.LIBRARY || []).concat(window.LOCAL_LIBRARY || []);
    return entries.filter(function (entry) {
    const fingerprint = entry.source && entry.source.fingerprint;
    if (seen.has(entry.id) || fingerprint && fingerprints.has(fingerprint)) return false;
    seen.add(entry.id);
    if (fingerprint) fingerprints.add(fingerprint);
    return true;
    });
  }
  window.CABINET_DATA = merge();

  async function load(url) {
    try {
      if (location.protocol === 'file:') {
        await new Promise(function (resolve) {
          const script = document.createElement('script');
          script.src = url;
          script.onload = resolve;
          script.onerror = resolve;
          document.head.appendChild(script);
        });
      } else {
        const response = await fetch(url, {cache: 'no-store'});
        if (response.ok) (0, eval)(await response.text());
      }
    } catch (error) {}
  }

  window.CABINET_READY = (async function () {
    await load('local/library.js');
    if (window.CABINET_LOCAL_PATH) await load(window.CABINET_LOCAL_PATH);
    seen.clear();
    fingerprints.clear();
    window.CABINET_DATA = merge();
  })();
})();

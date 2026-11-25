(function () {
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

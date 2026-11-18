/* Formularze z [data-zdjecia]: zmniejsza zdjęcia (do 1600 px, JPEG) przed wysłaniem — szybciej się wysyłają
   i mieszczą w limicie serwera. Bez obsługi createImageBitmap formularz wysyła się zwyczajnie. */
document.querySelectorAll('form[data-zdjecia]').forEach(function (form) {
  var inp = form.querySelector('input[type=file][name=zdjecia]');
  if (!inp) return;
  var max = +inp.dataset.max || 4;
  inp.addEventListener('change', function () {
    inp.setCustomValidity(inp.files.length > max ? 'Można dołączyć najwyżej ' + max + ' zdjęcia.' : '');
    inp.reportValidity();
  });
  form.addEventListener('submit', async function (e) {
    var btns = form.querySelectorAll('button');
    if (inp.files.length > max) { e.preventDefault(); inp.reportValidity(); return; }
    var duze = Array.from(inp.files).some(function (f) { return f.size > 600000; });
    if (!duze || !window.createImageBitmap || !window.fetch) {
      setTimeout(function () { btns.forEach(function (b) { b.disabled = true; }); }, 0);
      return;
    }
    e.preventDefault();
    var fd = new FormData(form);
    if (e.submitter && e.submitter.name) fd.set(e.submitter.name, e.submitter.value);
    btns.forEach(function (b) { b.disabled = true; });
    var btn = e.submitter || form.querySelector('button[type=submit]'), napis = btn.textContent;
    btn.textContent = 'Wysyłanie…';
    try {
      fd.delete('zdjecia');
      for (const f of inp.files) {
        if (f.size <= 600000) { fd.append('zdjecia', f, f.name); continue; }
        const img = await createImageBitmap(f), s = Math.min(1, 1600 / Math.max(img.width, img.height));
        const c = document.createElement('canvas'); c.width = Math.round(img.width * s); c.height = Math.round(img.height * s);
        c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
        const blob = await new Promise(function (r) { c.toBlob(r, 'image/jpeg', 0.85); });
        fd.append('zdjecia', blob, (f.name.replace(/\.[^.]+$/, '') || 'zdjecie') + '.jpg');
      }
      const r = await fetch(form.action, { method: 'POST', body: fd, credentials: 'same-origin' });
      const html = await r.text();
      history.replaceState(null, '', r.url);
      document.open(); document.write(html); document.close();
    } catch (err) {
      btns.forEach(function (b) { b.disabled = false; }); btn.textContent = napis;
      alert('Nie udało się przygotować zdjęć — spróbuj mniejszych plików.');
    }
  });
});

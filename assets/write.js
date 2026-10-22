// "Write to Wasem": show the fields that fit the chosen type, validate, and send through Web3Forms
(function () {
  var form = document.getElementById('write-form');
  if (!form) return;
  var F = form.elements;
  var ENDPOINT = 'https://api.web3forms.com/submit';
  var ACCESS_KEY = 'cc3765b8-bd37-4b51-abe3-c44ed27d2359'; // public by design; tied to wasemabdelhady@gmail.com
  var status = form.querySelector('.status'), send = form.querySelector('.send');
  var msgLabel = document.getElementById('msg-label');
  var labels = { 'رأي': 'رأيك', 'فكرة أغنية': 'الفكرة أو الحكاية', 'تعاون': 'عرّفنا بنفسك وبشغلك' };

  function sync() {
    var t = F.type.value;
    form.querySelectorAll('[data-for]').forEach(function (el) { el.hidden = el.dataset.for !== t; });
    msgLabel.textContent = labels[t];
  }
  form.querySelectorAll('input[name="type"]').forEach(function (r) { r.addEventListener('change', sync); });
  sync();

  function say(text, kind) { status.textContent = text; status.className = 'status' + (kind ? ' is-' + kind : ''); }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    if (F._honey.value) return;
    var t = F.type.value, bad = null;
    ['name', 'contact', 'message'].forEach(function (n) {
      var el = F[n], empty = !el.value.trim();
      el.classList.toggle('is-invalid', empty);
      if (empty && !bad) bad = el;
    });
    if (bad) { say('كمّل الخانات اللي بالأحمر', 'err'); bad.focus(); return; }
    if (t === 'تعاون' && !F.consent.checked) { say('محتاج توافق على جملة الحقوق قبل ما تبعت', 'err'); F.consent.focus(); return; }

    var data = {
      access_key: ACCESS_KEY,
      subject: 'رسالة من الموقع: ' + t + ' — ' + F.name.value.trim(),
      from_name: 'wasemabdelhady.com',
      'النوع': t,
      'الاسم': F.name.value.trim(),
      'التواصل': F.contact.value.trim(),
      'الرسالة': F.message.value.trim()
    };
    if (t === 'رأي' && F.song.value) data['الأغنية'] = F.song.value;
    if (t === 'تعاون') { data['الدور'] = F.role.value; if (F.link.value.trim()) data['اللينك'] = F.link.value.trim(); data['الحقوق'] = 'وافق'; }

    send.disabled = true; say('بنبعت…');
    fetch(ENDPOINT, { method: 'POST', headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' }, body: JSON.stringify(data) })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok || !j.success) throw new Error(j.message || r.status); }); })
      .then(function () {
        form.reset(); sync(); say('وصلت ✓ شكراً إنك كتبتلي', 'ok');
      })
      .catch(function () {
        say('حصلت مشكلة في الإرسال. جرّب تاني، أو ابعت على wasemabdelhady@gmail.com', 'err');
      })
      .then(function () { send.disabled = false; });
  });
})();

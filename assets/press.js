// Copy each bio block as plain text; fall back to selecting it when the clipboard is unavailable
document.querySelectorAll('[data-copy-scope]').forEach(function (box) {
  var btn = box.querySelector('.copy'), label = btn.querySelector('span'), orig = label.textContent;
  var textEl = box.querySelector('.bio__text');
  function selectText() {
    var r = document.createRange(); r.selectNodeContents(textEl);
    var sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
  }
  btn.addEventListener('click', function () {
    var text = [].map.call(textEl.querySelectorAll('p'), function (p) { return p.textContent; }).join('\n\n');
    function done() {
      label.textContent = box.lang === 'en' ? 'Copied' : 'اتنسخ';
      setTimeout(function () { label.textContent = orig; }, 1800);
    }
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, selectText);
    else selectText();
  });
});

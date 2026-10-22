// "Share a line": tap a lyric line to get a 1080×1920 story image (song colours, cover, signature, link)
(function () {
  var lyrics = document.querySelector('.lyrics');
  var dlg = document.querySelector('dialog.share');
  if (!lyrics || !dlg || !document.createElement('canvas').getContext) return;

  var page = document.body.dataset;            // slug, ar, en
  var css = getComputedStyle(document.body);
  var AMB = css.getPropertyValue('--amb').trim() || '#e0a458';
  var ACC = css.getPropertyValue('--acc').trim() || '#e0a458';
  var GOLD = '#e0a458', TEXT = '#f5efe6', MUTED = 'rgba(245,239,230,.62)';
  var W = 1080, H = 1920;
  var img = dlg.querySelector('img'), shareBtn = dlg.querySelector('.share__send'), saveBtn = dlg.querySelector('.share__save');
  var blob = null, fileName = 'wasem-' + page.slug + '.png';
  var sigFont = null;

  function loadSigFont() {
    if (sigFont) return sigFont;
    var f = new FontFace('Corinthia', 'url(https://fonts.gstatic.com/s/corinthia/v13/wEO6EBrAnchaJyPMHE097d8v1A.ttf)', { weight: '700' });
    sigFont = f.load().then(function (ff) { document.fonts.add(ff); return true; }).catch(function () { return false; });
    return sigFont;
  }

  function loadCover() {
    return new Promise(function (res) {
      var c = new Image();
      c.onload = function () { res(c); };
      c.onerror = function () { res(null); };
      c.src = '/covers/' + page.slug + '.webp';
    });
  }

  function wrap(ctx, text, maxW) {
    var words = text.split(' '), lines = [], cur = '';
    words.forEach(function (w) {
      var t = cur ? cur + ' ' + w : w;
      if (ctx.measureText(t).width > maxW && cur) { lines.push(cur); cur = w; } else cur = t;
    });
    if (cur) lines.push(cur);
    return lines;
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }

  function draw(line, cover, hasSig) {
    var cv = document.createElement('canvas'); cv.width = W; cv.height = H;
    var ctx = cv.getContext('2d');
    ctx.fillStyle = '#0a0a0a'; ctx.fillRect(0, 0, W, H);
    var g = ctx.createRadialGradient(W / 2, 760, 0, W / 2, 760, 900);
    g.addColorStop(0, AMB + 'aa'); g.addColorStop(.5, AMB + '33'); g.addColorStop(1, 'transparent');
    ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
    var g2 = ctx.createRadialGradient(W / 2, 1350, 0, W / 2, 1350, 700);
    g2.addColorStop(0, ACC + '30'); g2.addColorStop(1, 'transparent');
    ctx.fillStyle = g2; ctx.fillRect(0, 0, W, H);

    // cover
    var cs = 420, cx = (W - cs) / 2, cy = 230;
    if (cover) {
      ctx.save(); roundRect(ctx, cx, cy, cs, cs, 28); ctx.clip(); ctx.drawImage(cover, cx, cy, cs, cs); ctx.restore();
      ctx.save(); roundRect(ctx, cx, cy, cs, cs, 28); ctx.strokeStyle = ACC + '66'; ctx.lineWidth = 2; ctx.stroke(); ctx.restore();
    }

    // the line
    ctx.direction = 'rtl'; ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
    var size = 104, lines;
    do { ctx.font = '700 ' + size + 'px "Reem Kufi", "Cairo", sans-serif'; lines = wrap(ctx, line, 900); size -= 6; } while (lines.length > 4 && size > 56);
    size += 6;
    ctx.font = '700 ' + size + 'px "Reem Kufi", "Cairo", sans-serif';
    // balance the lines so the last one is never a lone word
    var n = lines.length, lo = 300, hi = 900;
    while (n > 1 && hi - lo > 8) { var mid = (lo + hi) / 2; if (wrap(ctx, line, mid).length > n) lo = mid; else hi = mid; }
    lines = wrap(ctx, line, hi);
    var lh = size * 1.45, top = 1080 - (lines.length - 1) * lh / 2;
    ctx.fillStyle = GOLD; ctx.fillRect(W / 2 - 40, top - size - 70, 80, 4);
    ctx.fillStyle = TEXT;
    lines.forEach(function (l, i) { ctx.fillText(l, W / 2, top + i * lh); });

    // song name
    var y = top + (lines.length - 1) * lh + 120;
    ctx.fillStyle = GOLD; ctx.font = '700 50px "Reem Kufi", sans-serif'; ctx.fillText('— ' + page.ar, W / 2, y);
    ctx.direction = 'ltr'; ctx.fillStyle = MUTED; ctx.font = '700 26px "Syne", sans-serif';
    ctx.fillText(page.en.toUpperCase().split('').join(' '), W / 2, y + 52);

    // signature + link
    ctx.fillStyle = GOLD;
    if (hasSig) { ctx.font = '700 104px "Corinthia", cursive'; ctx.fillText('Wasem Abdelhady', W / 2, 1690); }
    else { ctx.font = '800 44px "Syne", sans-serif'; ctx.fillText('WASEM ABDELHADY', W / 2, 1680); }
    ctx.fillStyle = MUTED; ctx.font = '700 28px "Syne", sans-serif';
    ctx.fillText('wasemabdelhady.com/' + page.slug, W / 2, 1790);
    return cv;
  }

  function open(line) {
    img.removeAttribute('src'); dlg.classList.add('is-loading');
    if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
    Promise.all([loadCover(), loadSigFont(), document.fonts.ready]).then(function (r) {
      var cv = draw(line, r[0], r[1]);
      cv.toBlob(function (b) {
        blob = b; img.src = URL.createObjectURL(b); dlg.classList.remove('is-loading');
        var file = new File([b], fileName, { type: 'image/png' });
        shareBtn.hidden = !(navigator.canShare && navigator.canShare({ files: [file] }));
      }, 'image/png');
    });
  }

  lyrics.addEventListener('click', function (e) {
    var ln = e.target.closest('.ln'); if (ln) open(ln.textContent.trim());
  });
  lyrics.addEventListener('keydown', function (e) {
    var ln = e.target.closest('.ln');
    if (ln && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); open(ln.textContent.trim()); }
  });

  shareBtn.addEventListener('click', function () {
    if (!blob) return;
    var file = new File([blob], fileName, { type: 'image/png' });
    navigator.share({ files: [file], url: location.origin + '/' + page.slug + '/' }).catch(function () {});
  });
  saveBtn.addEventListener('click', function () {
    if (!blob) return;
    var a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = fileName;
    document.body.appendChild(a); a.click(); a.remove();
  });
  dlg.querySelector('.share__close').addEventListener('click', function () { dlg.close ? dlg.close() : dlg.removeAttribute('open'); });
  dlg.addEventListener('click', function (e) { if (e.target === dlg) dlg.close(); });
})();

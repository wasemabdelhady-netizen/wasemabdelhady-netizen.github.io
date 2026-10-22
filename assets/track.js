// Meta Pixel + TikTok Pixel: PageView on load, plus events for streaming, contact and social clicks.
// Both vendor scripts are injected after the page has loaded (idle time), so they never delay rendering;
// calls made before that are queued by the stubs below and sent once the scripts arrive.
(function () {
  var META_ID = '1107327032293189';
  var TIKTOK_ID = 'DBD9N2BC77U4PGELA870';

  // --- Meta Pixel stub (official snippet, minus the immediate script tag) ---
  if (!window.fbq) {
    var n = window.fbq = function () { n.callMethod ? n.callMethod.apply(n, arguments) : n.queue.push(arguments); };
    if (!window._fbq) window._fbq = n;
    n.push = n; n.loaded = true; n.version = '2.0'; n.queue = [];
  }
  fbq('init', META_ID);
  fbq('track', 'PageView');

  // --- TikTok Pixel stub (official snippet, minus the immediate script tag) ---
  window.TiktokAnalyticsObject = 'ttq';
  var ttq = window.ttq = window.ttq || [];
  ttq.methods = ['page', 'track', 'identify', 'instances', 'debug', 'on', 'off', 'once', 'ready', 'alias', 'group', 'enableCookie', 'disableCookie', 'holdConsent', 'revokeConsent', 'grantConsent'];
  ttq.setAndDefer = function (t, e) { t[e] = function () { t.push([e].concat(Array.prototype.slice.call(arguments, 0))); }; };
  for (var i = 0; i < ttq.methods.length; i++) ttq.setAndDefer(ttq, ttq.methods[i]);
  ttq.instance = function (t) { for (var e = ttq._i[t] || [], n = 0; n < ttq.methods.length; n++) ttq.setAndDefer(e, ttq.methods[n]); return e; };
  ttq.load = function (e, n) {
    var r = 'https://analytics.tiktok.com/i18n/pixel/events.js';
    ttq._i = ttq._i || {}; ttq._i[e] = []; ttq._i[e]._u = r;
    ttq._t = ttq._t || {}; ttq._t[e] = +new Date();
    ttq._o = ttq._o || {}; ttq._o[e] = n || {};
    ttq._src = r + '?sdkid=' + e + '&lib=ttq';
  };
  ttq.load(TIKTOK_ID);
  ttq.page();

  function inject(src) {
    var s = document.createElement('script');
    s.async = true; s.src = src;
    document.head.appendChild(s);
  }
  function loadVendors() {
    inject('https://connect.facebook.net/en_US/fbevents.js');
    inject(ttq._src);
  }
  function whenIdle() { (window.requestIdleCallback || function (f) { setTimeout(f, 1); })(loadVendors, { timeout: 3000 }); }
  if (document.readyState === 'complete') whenIdle(); else addEventListener('load', whenIdle, { once: true });

  // --- What was clicked ---
  var SONGS = { // ids from the platform links → song name (fallback when the page gives no context)
    '1ZUtBNTMqPlJMOqx9mjsww': 'Ba2olak Eh', '6820402630': 'Ba2olak Eh', '1306308384': 'Ba2olak Eh', 'SYH-gLZuNs4': 'Ba2olak Eh',
    '0DWMpXgS6Q3hBbeqWvTqvG': 'Mesh Le3ba', '6799878765': 'Mesh Le3ba', '1294597707': 'Mesh Le3ba', '2M8zF7OjcY8': 'Mesh Le3ba',
    '61N58LukOCpYvQfLchpFJu': 'Enta El Habib', '1781385640': 'Enta El Habib', '1187145973': 'Enta El Habib', 'XPX0FuAgUc4': 'Enta El Habib'
  };

  function songFromPage(el) {
    var card = el.closest('article.song, #latest, .video');
    var t = card && card.querySelector('.song-title [lang="en"], .video__label [lang="en"]');
    if (t) return t.textContent.trim();
    if (el.matches && el.matches('.video') && el.querySelector('[lang="en"]')) return el.querySelector('[lang="en"]').textContent.trim();
    return document.body.dataset.en || '';
  }
  function songFromUrl(url) {
    for (var id in SONGS) if (url.indexOf(id) !== -1) return SONGS[id];
    return '';
  }

  function classify(url) {
    var u; try { u = new URL(url, location.href); } catch (e) { return null; }
    var h = u.hostname.replace(/^www\./, ''), p = u.pathname;
    if (u.protocol === 'mailto:') return { kind: 'contact', platform: 'Email' };
    if (h === 'wa.me' || h === 'api.whatsapp.com') return { kind: 'contact', platform: 'WhatsApp' };
    if (h === 'instagram.com') return { kind: 'social', platform: 'Instagram' };
    if (h === 'tiktok.com') return { kind: 'social', platform: 'TikTok' };
    if (h === 'facebook.com') return { kind: 'social', platform: 'Facebook' };
    if (h === 'youtube.com' && /^\/(@|channel\/|c\/|user\/)/.test(p)) return { kind: 'social', platform: 'YouTube' };
    // artist profiles on streaming services are "follow" intent, not a song play
    if (h === 'open.spotify.com' && /^\/artist\//.test(p)) return { kind: 'social', platform: 'Spotify' };
    if (h === 'music.apple.com' && /\/artist\//.test(p)) return { kind: 'social', platform: 'Apple Music' };
    if (h === 'play.anghami.com' && /^\/artist\//.test(p)) return { kind: 'social', platform: 'Anghami' };
    if (h === 'open.spotify.com') return { kind: 'stream', platform: 'Spotify' };
    if (h === 'music.apple.com') return { kind: 'stream', platform: 'Apple Music' };
    if (h === 'play.anghami.com') return { kind: 'stream', platform: 'Anghami' };
    if (h === 'youtube.com' || h === 'youtu.be' || h === 'youtube-nocookie.com') return { kind: 'stream', platform: 'YouTube' };
    return null;
  }

  function send(c, song) {
    if (c.kind === 'stream') {
      var name = (song || 'Wasem Abdelhady') + ' - ' + c.platform;
      fbq('track', 'ViewContent', { content_name: name, content_category: 'Music', content_type: 'product' });
      ttq.track('ClickButton', { content_name: name, content_type: 'product' });
    } else if (c.kind === 'contact') {
      fbq('track', 'Contact', { content_name: c.platform });
      ttq.track('Contact', { content_name: c.platform });
    } else if (c.kind === 'social') {
      fbq('track', 'Lead', { content_name: 'Follow - ' + c.platform });
      ttq.track('ClickButton', { content_name: 'Follow', content_category: c.platform });
    }
  }

  // Capture phase so the event is recorded before the browser leaves the page
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href]');
    if (a) {
      var c = classify(a.href);
      if (c) send(c, c.kind === 'stream' ? (songFromUrl(a.href) || songFromPage(a)) : '');
      return;
    }
    // in-page YouTube player (click-to-load button)
    var v = e.target.closest && e.target.closest('button.video[data-yt]');
    if (v) send({ kind: 'stream', platform: 'YouTube' }, songFromUrl(v.dataset.yt) || songFromPage(v));
  }, true);
})();

// Song pages: audio preview with a progress ring, and click-to-load YouTube
(function () {
  var btn = document.querySelector('.play');
  if (btn) {
    var audio = new Audio(), raf = 0, C = 138.23, bar = btn.querySelector('.play__bar');
    audio.preload = 'none';
    audio.src = btn.dataset.audio;
    function tick() {
      if (audio.duration) bar.style.strokeDashoffset = C * (1 - audio.currentTime / audio.duration);
      raf = requestAnimationFrame(tick);
    }
    function stop() { btn.classList.remove('is-playing'); cancelAnimationFrame(raf); }
    audio.addEventListener('ended', function () { stop(); bar.style.strokeDashoffset = C; });
    btn.addEventListener('click', function () {
      if (audio.paused) {
        audio.play().then(function () { btn.classList.add('is-playing'); raf = requestAnimationFrame(tick); }).catch(stop);
      } else { audio.pause(); stop(); }
    });
  }

  var v = document.querySelector('button.video');
  if (v) {
    var id = v.dataset.yt;
    v.style.backgroundImage = 'url(https://i.ytimg.com/vi/' + id + '/hqdefault.jpg)';
    v.addEventListener('click', function () {
      var f = document.createElement('iframe');
      f.className = 'video';
      f.src = 'https://www.youtube-nocookie.com/embed/' + id + '?autoplay=1&rel=0';
      f.title = v.getAttribute('aria-label');
      f.allow = 'autoplay; encrypted-media; picture-in-picture; fullscreen';
      f.allowFullscreen = true;
      v.replaceWith(f);
    });
  }
})();

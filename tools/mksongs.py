"""Generate one static page per song (doubles as the release's smart link)."""
import json, html, os, sys

ROOT = sys.argv[1]
SPRITE = open(sys.argv[2]).read()
SITE = 'https://wasemabdelhady.com'

SONGS = [
  dict(slug='ba2olak-eh', ar='بقولك ايه', en='Ba2olak Eh', date='2026-10-15', year='2026',
       amb='#2b56a0', acc='#e73540',
       spotify='https://open.spotify.com/album/1ZUtBNTMqPlJMOqx9mjsww',
       apple='https://music.apple.com/eg/album/%D8%A8%D9%82%D9%88%D9%84%D9%83-%D8%A7%D9%8A%D9%87-ba2olak-eh-single/6820402630',
       anghami='https://play.anghami.com/song/1306308384', yt='SYH-gLZuNs4',
       desc='أغنية «بقولك ايه» لوسيم عبد الهادي (2026): الكلمات كاملة، الكليب، والكريدتس. كلمات بسام محمود، ألحان وتوزيع وسيم.',
       lyricist='Bassam Mahmoud', composer=None,
       credits=[('كلمات','بسام محمود'),('ألحان وتوزيع','وسيم'),('ميكس وماستر','لاتينو'),('فيديو ومونتاج','وسيم')],
       lyrics=[
         ['بقولك ايه كفايه كلام مفيش ولا كلمة هتداوي','باين في عنيك كلامك كله كدب وعلى الفراق ناوي','!جرحتك؟'],
         ['*كنت فين وانا قلبي بيبكي','*من الفراق دلوقتي بتشكي','*بس قلبي حبيبي خلاص اكتفى','*!هنبعد','*ولو في يوم حنيت مش هرجع','*للي حبه في قلبي بيوجع','*الحكايه حبيبي خلاص انتهت'],
         ['انا الغلطان','فضلت اكدب الاحساس ده جوايا','لحد ما بان','متستاهلش تفضل ثانية ويايا','!جرحتك'],
       ], chorus_note=''),
  dict(slug='mesh-le3ba', ar='مش لعبة', en='Mesh Le3ba', date='2026-08-13', year='2026',
       amb='#04899a', acc='#e78e22',
       spotify='https://open.spotify.com/track/0DWMpXgS6Q3hBbeqWvTqvG',
       apple='https://music.apple.com/eg/song/%D9%85%D8%B4-%D9%84%D8%B9%D8%A8%D8%A9/6799878765',
       anghami='https://play.anghami.com/song/1294597707', yt='2M8zF7OjcY8',
       desc='أغنية «مش لعبة» لوسيم عبد الهادي (2026): الكلمات كاملة، الكليب، والكريدتس. كلمات وألحان بسام محمود، توزيع وسيم.',
       lyricist='Bassam Mahmoud', composer='Bassam Mahmoud',
       credits=[('كلمات وألحان','بسام محمود'),('توزيع','وسيم'),('ميكس وماستر','لاتينو'),('فيديو ومونتاج','وسيم')],
       lyrics=[
         ['*انا مش لعبة بتلعبها','*وفاكر ان انت هتكسبها','*بتبعد وتقرب في قلبي','*بتجرح وتجرب'],
         ['ده انا من يوم معرفتك وحالة قلبي بتتأخر','بيوعدني ويخلف وألومه ويقولي بهزر'],
         ['ياه دي كانت ايام سنين وياه','ضاع مني سنين وانا بستناه','بقى بعد ما قلبي عرف يتخطى','جي يقولي فرقي كان غلطة','دنا من كتر سخفت اللقطة','فضلت اشرحله ازاي بنساه'],
         ['ده لو السما بتمطر حنية تشتري شمسية','انا مش ليك تاني يا حبيبي وطبعي إن انت مش ليّا','دنا لو يوم تاني شوفتك هغير سكتي واتنكر','ولو جيت على بال هعمل عملية وابطل افكر'],
       ], chorus_note=''),
  dict(slug='enta-el-habib', ar='انت الحبيب', en='Enta El Habib', date='2024-11-19', year='2024',
       amb='#7d848c', acc='#e6e6e6',
       spotify='https://open.spotify.com/track/61N58LukOCpYvQfLchpFJu',
       apple='https://music.apple.com/eg/song/enta-el-habib-%D8%A7%D9%86%D8%AA-%D8%A7%D9%84%D8%AD%D8%A8%D9%8A%D8%A8/1781385640',
       anghami='https://play.anghami.com/song/1187145973', yt='XPX0FuAgUc4',
       desc='أغنية «انت الحبيب» لوسيم عبد الهادي (2024): الكلمات كاملة، الكليب، والكريدتس. كلمات وألحان بسام محمود، توزيع وميكس وسيم.',
       lyricist='Bassam Mahmoud', composer='Bassam Mahmoud',
       credits=[('كلمات وألحان','بسام محمود'),('توزيع وميكس','وسيم'),('ستيم ماستر','لاتينو'),('فيديو ومونتاج','وسيم')],
       lyrics=[
         ['عمّال تقول وتعيد في كلام','مش لاقي حاجة تقولهالي','طب كنت قولي حتى سلام','حسسني إن انا عندك غالي','مشيت وسبتني للأيام','وسهرت مع نفسي ليالي','دلوقتي جي تقولي بحبك','عارف انت ايه بالنسبه لي'],
         ['*انت الحبيب اللي جرح','*انت الآلام وقت الفرح','*انت العذاب وقت الهنا','*انت اللي خُنت حبنا'],
         ['قلبي اللي كان بيحبك مات','انت اللي دوست عليه بإيدك','بلاش تفتح في اللي فات','الذكريات مش هتفيدك','معدش فاضل للحب باب','كله اتقفل تسلم ايدك','امشي وسبني من سكات','شوف حد غيري ياخد بإيدك'],
       ], chorus_note=''),
]

e = html.escape

def platform(href, icon, name, main=False):
    cls = 'platform platform--main' if main else 'platform'
    return (f'<a class="{cls}" href="{e(href)}" target="_blank" rel="noopener">'
            f'<svg aria-hidden="true"><use href="#i-{icon}"/></svg><b>{name}</b><i>اسمع ↗</i></a>')

def page(s):
    url = f'{SITE}/{s["slug"]}/'
    lyrics_text = '\n\n'.join('\n'.join(l.lstrip('*!') for l in st) for st in s['lyrics'])
    comp = ({'@id': f'{SITE}/#wasem'} if s['composer'] is None
            else {'@type': 'Person', 'name': s['composer']})
    ld = {
      '@context': 'https://schema.org', '@type': 'MusicRecording',
      '@id': url + '#recording', 'name': f'{s["ar"]} ({s["en"]})', 'url': url,
      'datePublished': s['date'], 'image': f'{SITE}/covers/{s["slug"]}.jpg',
      'byArtist': {'@type': ['Person', 'MusicGroup'], '@id': f'{SITE}/#wasem', 'name': 'Wasem Abdelhady'},
      'recordingOf': {'@type': 'MusicComposition', 'name': s['ar'],
                      'lyricist': {'@type': 'Person', 'name': s['lyricist']}, 'composer': comp,
                      'lyrics': {'@type': 'CreativeWork', 'text': lyrics_text, 'inLanguage': 'ar'}},
      'sameAs': [s['spotify'], s['apple'], s['anghami'], f'https://www.youtube.com/watch?v={s["yt"]}'],
    }
    stanzas = []
    for st in s['lyrics']:
        chorus = all(l.startswith('*') for l in st)
        # '!' marks a hook word (e.g. «جرحتك» / «هنبعد»): set apart in the song's accent colour
        # every line is tappable (share as a story); hook lines also get the accent style
        def ln(l):
            t = e(l.lstrip('*!'))
            cls = 'ln hook' if l.lstrip('*').startswith('!') else 'ln'
            return f'<span class="{cls}" role="button" tabindex="0">{t}</span>'
        body = '<br>'.join(ln(l) for l in st)
        stanzas.append(f'        <p{" class=\"chorus\"" if chorus else ""}>{body}</p>')
    if s['chorus_note']:
        stanzas.append(f'        <small>{e(s["chorus_note"])}</small>')
    credits = '\n'.join(f'        <div><dt>{e(a)}</dt><dd>{e(b)}</dd></div>' for a, b in s['credits'])
    others = [o for o in SONGS if o is not s]
    more = '\n'.join(
        f'        <a href="/{o["slug"]}/"><img src="/covers/{o["slug"]}-400.webp" alt="غلاف {e(o["ar"])}" loading="lazy" width="400" height="400">'
        f'<b>{e(o["ar"])}</b><span lang="en" dir="ltr">{e(o["en"])} · {o["year"]}</span></a>' for o in others)
    title = f'{s["ar"]} — وسيم عبد الهادي | {s["en"]} — Wasem Abdelhady'
    return f'''<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>{e(title)}</title>
<meta name="description" content="{e(s['desc'])}">
<link rel="canonical" href="{url}">
<meta name="theme-color" content="#0a0a0a">
<link rel="icon" type="image/png" sizes="32x32" href="/icon-32.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta property="og:type" content="music.song">
<meta property="og:title" content="{e(s['ar'])} — وسيم عبد الهادي">
<meta property="og:description" content="اسمعها على Spotify · Apple Music · Anghami · YouTube">
<meta property="og:url" content="{url}">
<meta property="og:image" content="{SITE}/covers/{s['slug']}.jpg">
<meta name="twitter:card" content="summary_large_image">
<script type="application/ld+json">
{json.dumps(ld, ensure_ascii=False, indent=2)}
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700&family=Reem+Kufi:wght@500;700&family=Syne:wght@700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/assets/song.css">
</head>
<body style="--amb:{s['amb']};--acc:{s['acc']}" data-slug="{s['slug']}" data-ar="{e(s['ar'])}" data-en="{e(s['en'])}">
{SPRITE}
  <header class="wrap top">
    <a href="/"><img src="/icon-192.png" alt="" width="28" height="28">وسيم عبد الهادي <span lang="en" dir="ltr">WASEM</span></a>
  </header>

  <main class="wrap">
    <section class="release">
      <div class="halo">
        <div class="cover"><img src="/covers/{s['slug']}.webp" alt="غلاف {e(s['ar'])} — {e(s['en'])}" width="900" height="900" fetchpriority="high"></div>
        <button class="play" type="button" data-audio="/audio/{s['slug']}.mp3" aria-label="اسمع مقطع من {e(s['ar'])}"><svg class="play__ring" viewBox="0 0 48 48" aria-hidden="true"><circle cx="24" cy="24" r="22"/><circle class="play__bar" cx="24" cy="24" r="22"/></svg><svg class="play__icon" aria-hidden="true"><use href="#i-play"/></svg><svg class="play__pause" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z"/></svg></button>
      </div>
      <div>
        <h1>{e(s['ar'])} <span lang="en" dir="ltr">{e(s['en'])}</span></h1>
        <p class="meta"><span dir="ltr">Single · {s['year']} · WA Records</span></p>
        <nav class="platforms" aria-label="اسمع الأغنية">
          {platform(s['spotify'], 'spotify', 'Spotify', main=True)}
          {platform(s['apple'], 'apple', 'Apple Music')}
          {platform(s['anghami'], 'anghami', 'Anghami')}
          {platform('https://www.youtube.com/watch?v=' + s['yt'], 'youtube', 'YouTube')}
        </nav>
      </div>
    </section>

    <section class="block" aria-labelledby="video-h">
      <h2 id="video-h">الكليب <span lang="en" dir="ltr">VIDEO</span></h2>
      <button class="video" type="button" data-yt="{s['yt']}" aria-label="شغّل كليب {e(s['ar'])}"><span class="video__play"><svg aria-hidden="true"><use href="#i-play"/></svg></span></button>
    </section>

    <section class="block" aria-labelledby="lyrics-h">
      <h2 id="lyrics-h">الكلمات <span lang="en" dir="ltr">LYRICS</span></h2>
      <p class="share-hint">دوس على أي سطر وشاركه ستوري <span lang="en" dir="ltr">Tap a line to share it</span></p>
      <div class="lyrics">
{chr(10).join(stanzas)}
      </div>
    </section>

    <section class="block" aria-labelledby="credits-h">
      <h2 id="credits-h">الكريدتس <span lang="en" dir="ltr">CREDITS</span></h2>
      <dl class="credits">
        <div><dt>غناء</dt><dd>وسيم</dd></div>
{credits}
        <div><dt>إصدار</dt><dd lang="en" dir="ltr">WA Records</dd></div>
      </dl>
    </section>

    <section class="block" aria-labelledby="more-h">
      <h2 id="more-h">أغاني تانية <span lang="en" dir="ltr">MORE</span></h2>
      <div class="more">
{more}
      </div>
      <a class="home-link" href="/">← كل حاجة عن وسيم · wasemabdelhady.com</a>
    </section>
  </main>

  <dialog class="share" aria-label="شارك السطر">
    <div class="share__box">
      <button class="share__close" type="button" aria-label="اقفل">✕</button>
      <div class="share__preview"><img alt="صورة الستوري"><span class="share__wait">بنجهّز الصورة…</span></div>
      <div class="share__actions">
        <button class="share__send" type="button" hidden>شارك</button>
        <button class="share__save" type="button">حمّل الصورة</button>
      </div>
    </div>
  </dialog>

  <footer class="footer">
    <p dir="ltr">© 2026 Wasem Abdelhady · وسيم عبد الهادي</p>
    <p dir="ltr">℗ WA Records</p>
  </footer>

  <script src="/assets/song.js" defer></script>
  <script src="/assets/share.js" defer></script>
  <!-- Cloudflare Web Analytics (visitor counts, no cookies) --><script defer src='https://static.cloudflareinsights.com/beacon.min.js' data-cf-beacon='{{"token": "b26b5bf0eb62472c987c27890a42bb25"}}'></script>
</body>
</html>
'''

for s in SONGS:
    d = os.path.join(ROOT, s['slug']); os.makedirs(d, exist_ok=True)
    open(os.path.join(d, 'index.html'), 'w').write(page(s))
    print('wrote', s['slug'])

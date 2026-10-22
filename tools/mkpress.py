"""Generate /press/index.html (EPK)."""
import sys
root, sprite_path = sys.argv[1], sys.argv[2]
sprite = open(sprite_path).read()
extra = ('    <symbol id="i-copy" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" d="M9 9h11v11H9z M5 15H4V4h11v1"/></symbol>\n'
         '    <symbol id="i-dl" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" d="M12 4v11m-5-5 5 5 5-5M5 20h14"/></symbol>\n')
sprite = sprite.replace('  </svg>\n', extra + '  </svg>\n', 1)

short_ar = 'وسيم عبد الهادي مطرب وملحّن مصري مستقل من القاهرة. بيشتغل على أغانيه من الفكرة لحد ما توصل للناس: الغناء والتوزيع وصناعة الفيديو، بصوت بيجمع بين البوب المصري والروح الشرقية. أحدث إصداراته «بقولك ايه» (2026)، من ألحانه وتوزيعه.'
short_en = 'Wasem Abdelhady is an independent Egyptian singer and composer based in Cairo. He shapes every song from first idea to final release: vocals, arrangement and visuals, with a sound that blends Egyptian pop and an oriental soul. His latest single, “Ba2olak Eh” (2026), is his own composition and arrangement, out now on WA Records.'
long_ar = [
  'وسيم عبد الهادي مطرب وملحّن مصري مستقل من القاهرة، بيقدّم بوب مصري بروح شرقية، وبيشارك في كل مراحل أغانيه: الغناء والتوزيع وصناعة الفيديو.',
  'بعد سلسلة من الكوفرات، قدّم أول أغنية خاصة بيه «انت الحبيب» (2024)، كلمات وألحان بسام محمود، ومن توزيع وميكس وسيم نفسه. وفي 2026 قدّم «مش لعبة» بنفس الشراكة مع بسام محمود، وبعدها «بقولك ايه»، أول إصدار من ألحانه وتوزيعه. وفي الميكس والماستر، بيتعاون مع لاتينو على أغانيه.',
  'وبجانب أغانيه، وسيم موزّع موسيقي اشتغل مع فنانين كتير، وده اللي بيدّي أغانيه صوت مدروس من أول نوتة.',
  'وسيم بيصنع الفيديوهات بتاعة أغانيه بنفسه، من الفكرة للمونتاج، وده اللي بيدّي إصداراته شكل بصري واحد متماسك. أغانيه بتصدر عن <bdi>WA Records</bdi>، ومتاحة على سبوتيفاي وأبل ميوزك وأنغامي ويوتيوب.',
]
long_en = [
  'Wasem Abdelhady is an independent Egyptian singer and composer based in Cairo. His music is Egyptian pop with an oriental soul, and he is involved in every stage of his releases: vocals, arrangement and visuals.',
  'After a series of covers, his first original single, “Enta El Habib” (2024), was written and composed by Bassam Mahmoud and arranged and mixed by Wasem himself. In 2026 the partnership continued with “Mesh Le3ba”, followed by “Ba2olak Eh”, his first single as composer and arranger. On the engineering side, he works with Latino on mixing and mastering.',
  'Beyond his own releases, Wasem is a music arranger who has worked with many artists, which gives his songs a carefully crafted sound from the very first note.',
  'Wasem creates the music videos for his songs himself, from concept to edit, giving his releases one consistent visual language. His music is released on WA Records and is available on Spotify, Apple Music, Anghami and YouTube.',
]
photos = [('hero.jpg', 'صورة رئيسية · Portrait', '1080 × 1448'), ('og.jpg', 'صورة أفقية · Landscape', '1200 × 630'),
          ('covers/ba2olak-eh.jpg', 'غلاف بقولك ايه', '900 × 900'), ('covers/mesh-le3ba.jpg', 'غلاف مش لعبة', '900 × 900'),
          ('covers/enta-el-habib.jpg', 'غلاف انت الحبيب', '800 × 800')]

def paras(ps): return '\n'.join('          <p>' + p + '</p>' for p in ps)
def bio(label, copy, text, en=False):
    attrs = ' lang="en" dir="ltr"' if en else ''
    return ('      <div class="bio"' + attrs + ' data-copy-scope>\n'
            '        <div class="bio__head"><h3>' + label + '</h3><button class="copy" type="button"><svg aria-hidden="true"><use href="#i-copy"/></svg><span>' + copy + '</span></button></div>\n'
            '        <div class="bio__text">\n' + text + '\n        </div>\n      </div>')
ph = '\n'.join('        <a class="photo" href="/' + f + '" download aria-label="تحميل الصورة"><img src="/' + f + '" alt="وسيم عبد الهادي" loading="lazy"><svg aria-hidden="true"><use href="#i-dl"/></svg></a>' for f, t, d in photos)

page = open(__file__.replace('mkpress.py', 'press.tpl.html')).read()
page = (page.replace('%%SPRITE%%', sprite)
            .replace('%%BIOS%%', '\n'.join([bio('عربي', 'انسخ', paras(long_ar)), bio('English', 'Copy', paras(long_en), True)]))
            .replace('%%PHOTOS%%', ph))
open(root + '/press/index.html', 'w').write(page)
print('wrote press')

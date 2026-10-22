// Renders og.jpg (1200x630 share card) from hero.jpg. Run from the repo root: node tools/og.mjs
import { chromium } from 'playwright';
import { readFileSync } from 'fs';
const HERO = readFileSync('hero.jpg').toString('base64');
const b = await chromium.launch({ executablePath: process.env.CHROME || undefined });
const p = await b.newPage({ viewport:{width:1200,height:630} });
await p.setContent(`<!doctype html><html><head>
<link href="https://fonts.googleapis.com/css2?family=Reem+Kufi:wght@500&family=Syne:wght@700;800&display=block" rel="stylesheet">
<style>
*{margin:0;box-sizing:border-box}
body{width:1200px;height:630px;background:#0a0a0a;position:relative;overflow:hidden;font-family:Syne}
.img{position:absolute;left:0;top:0;width:540px;height:630px;background:url(data:image/jpeg;base64,${HERO}) 50% 8%/cover}
.img::after{content:"";position:absolute;inset:0;background:linear-gradient(to right,rgba(10,10,10,0) 55%,#0a0a0a 100%)}
.t{position:absolute;left:560px;right:40px;top:0;bottom:0;display:flex;flex-direction:column;justify-content:center}
h1{font-weight:800;font-size:57px;line-height:.92;color:#f5efe6;text-transform:uppercase;letter-spacing:-.01em}
.ar{margin-top:18px;font-family:'Reem Kufi';font-weight:500;font-size:36px;color:#e0a458;direction:rtl;text-align:left}
.r{margin-top:14px;font-weight:700;font-size:16px;letter-spacing:.3em;color:rgba(245,239,230,.62)}
.u{position:absolute;left:560px;bottom:44px;font-weight:700;font-size:15px;letter-spacing:.12em;color:#e0a458}
</style></head><body><div class="img"></div>
<div class="t"><h1>Wasem<br>Abdelhady</h1><p class="ar">وسيم عبد الهادي</p><p class="r">SINGER · COMPOSER · ARRANGER</p></div>
<div class="u">WASEMABDELHADY.COM</div></body></html>`,{waitUntil:'networkidle'});
await p.evaluate(()=>document.fonts.ready);
await p.screenshot({path:'og.png'});
await b.close();

// End-to-end test of AdsDashboard.html in headless Chromium (file://, no server).
//   node test/test.mjs [out_dir]      (needs the `playwright` package)
import { createRequire } from "module";
import fs from "fs";
import path from "path";
import { fileURLToPath, pathToFileURL } from "url";

const require = createRequire(import.meta.url);
let pw;
for (const p of ["playwright", "/opt/node22/lib/node_modules/playwright"]) {
  try { pw = require(p); break; } catch (e) {}
}
const HERE = path.dirname(fileURLToPath(import.meta.url));
const FILE = path.resolve(HERE, "..", "AdsDashboard.html");
const OUT = path.resolve(process.argv[2] || path.join(HERE, "out"));
fs.mkdirSync(OUT, { recursive: true });

let failures = 0;
function ok(cond, msg) {
  console.log((cond ? "OK   " : "FAIL ") + msg);
  if (!cond) failures++;
}
const txt = (page, sel) => page.locator(sel).first().innerText();

const META_CSV = `﻿Reporting starts,Reporting ends,Campaign name,Ad set name,Ad name,Platform,Amount spent (EGP),Impressions,ThruPlays,Cost per ThruPlay (EGP),Instagram profile visits,Instagram follows,Post saves,Post shares
2026-10-24,2026-10-24,Train rain,Egypt 18-34,train + rain,instagram,100.50,"12,400","4,100",0.02,61,9,14,7
2026-10-25,2026-10-25,Train rain,Egypt 18-34,train + rain,facebook,20,"3,000",900,0.02,0,0,1,2
,,,,,,120.50,"15,400","5,000",,61,9,15,9`;
const TIKTOK_CSV = `Ad name,By Day,Ad group name,Cost,Impression,Video views at 2s,Video views,Profile visits,Profile visits rate,Paid followers,Shares,Saves
تعبت أشرح v2,2026-10-27,Egypt,80.00,20000,9000,15000,300,1.5%,210,25,40`;

const opts = { headless: true };
if (process.env.CHROME) opts.executablePath = process.env.CHROME;
const browser = await pw.chromium.launch(opts);
const ctx = await browser.newContext({ viewport: { width: 1366, height: 900 }, acceptDownloads: true });
const page = await ctx.newPage();
const errors = [];
page.on("pageerror", e => errors.push(String(e)));
page.on("console", m => { if (m.type() === "error") errors.push(m.text()); });
page.on("dialog", d => d.accept());
const url = pathToFileURL(FILE).href;
await page.goto(url);

// --- fonts / layout
ok(await page.evaluate(() => document.documentElement.dir === "rtl"), "page is RTL");
await page.evaluate(() => document.fonts.ready);
ok(await page.evaluate(() => document.fonts.check('700 16px "Cairo"') && [...document.fonts].some(f => f.family.replace(/"/g, "") === "Cairo" && f.status === "loaded")),
   "Cairo font embedded and loaded (offline)");
await page.screenshot({ path: path.join(OUT, "1-overview-seed.png"), fullPage: true });

// --- seed data
const rows = () => page.evaluate(k => JSON.parse(localStorage.getItem(k)).rows.length, "wasem-ads-dashboard-v1");
ok(await rows() === 3, "seed: 3 records saved to localStorage");
ok((await txt(page, "#card-tiktok .cpf")).includes("0.40"), "TikTok card: cost per follower 0.40");
ok((await txt(page, "#card-tiktok .followers")) === "354", "TikTok card: 354 followers");
ok((await txt(page, "#card-tiktok .spent")).includes("141.60"), "TikTok card: spent 141.60");
ok((await txt(page, "#card-instagram .spent")).includes("398.90"), "Instagram card: spent 398.90 (train+rain has no spend yet)");
ok((await txt(page, "#card-instagram .cpf")).includes("199.45"), "Instagram card: cost per follower 199.45");
ok((await page.locator("#card-instagram .cpf").getAttribute("style")).includes("--red"), "Instagram card CPF shown in red");

await page.click('nav button[data-tab="ads"]');
const adRow = name => page.locator("#ads-table tbody tr", { hasText: name });
ok(await adRow("تعبت أشرح").evaluate(tr => tr.classList.contains("best")), "ads table: TikTok row highlighted gold (best CPF)");
ok(await adRow("بتسمع كلامه").locator("td.cpf").evaluate(td => td.classList.contains("bad")), "ads table: «بتسمع كلامه» CPF in red");
ok((await adRow("بتسمع كلامه").locator("td.cpv").innerText()) === "2.89", "«بتسمع كلامه» cost per profile visit 2.89");
ok((await adRow("بتسمع كلامه").locator("td.cpm").innerText()) === "73.87", "«بتسمع كلامه» cost per 1000 views 73.87");
ok((await adRow("بتسمع كلامه").locator("td.follow").innerText()) === "1.45%", "«بتسمع كلامه» follow rate 1.45%");
ok((await adRow("train + rain").locator("td.cpf").innerText()) === "—", "train + rain: no results yet → —");
await page.screenshot({ path: path.join(OUT, "2-ads-seed.png"), fullPage: true });

// --- form: a new ad
await page.click('nav button[data-tab="add"]');
const fill = async (o) => {
  for (const [k, v] of Object.entries(o)) {
    const el = page.locator(`#form [name="${k}"]`);
    if (k === "platform") await el.selectOption(v); else await el.fill(String(v));
  }
};
await fill({ date: "2026-10-25", platform: "facebook", ad: "اختبار فيسبوك", reel: "ريل الاختبار", objective: "متابعين",
             audience: "مصر", budget: 100, spent: 50, views: 10000, visits: 100, followers: 40, saves: 30, shares: 20 });
ok((await txt(page, "#form-metrics")).includes("1.25"), "form shows live metrics (CPF 1.25)");
await page.click("#form-submit");
ok(await rows() === 4, "form: row added");
await page.click('nav button[data-tab="ads"]');
const fb = adRow("اختبار فيسبوك");
ok((await fb.locator("td.cpf").innerText()) === "1.25", "cost per follower = 50 / 40 = 1.25");
ok((await fb.locator("td.cpv").innerText()) === "0.50", "cost per profile visit = 50 / 100 = 0.50");
ok((await fb.locator("td.cpm").innerText()) === "5.00", "cost per 1,000 views = 50 / 10000 × 1000 = 5.00");
ok((await fb.locator("td.follow").innerText()) === "40.0%", "follow rate = 40 / 100 = 40.0%");
ok((await fb.locator("td.ss").innerText()) === "0.50%", "save+share rate = (30+20) / 10000 = 0.50%");

// --- a second check-in for the TikTok ad (cumulative mode: latest wins)
await page.click('nav button[data-tab="add"]');
await fill({ date: "2026-10-26", platform: "tiktok", ad: "تعبت أشرح" });
await page.locator('#form [name="ad"]').dispatchEvent("change");
ok((await page.locator('#form [name="reel"]').inputValue()) === "تعبت أشرح", "existing ad name pre-fills its reel");
await fill({ spent: 230, followers: 560, visits: 900, views: 40000 });
await page.click("#form-submit");
ok((await txt(page, "#card-tiktok .followers")) === "560", "check-in: TikTok card uses the latest totals (560)");
ok((await txt(page, "#card-tiktok .cpf")).includes("0.41"), "check-in: TikTok CPF 230 / 560 = 0.41");
await page.click('nav button[data-tab="ads"]');
ok((await adRow("تعبت أشرح").innerText()).includes("2 تشيك-إن"), "ads table: one row per ad with 2 check-ins");

// --- sorting
const cpfs = async () => (await page.locator("#ads-table tbody td.cpf").allInnerTexts()).map(t => parseFloat(t.replace(/,/g, ""))).filter(n => !isNaN(n));
let v = await cpfs();
ok(v.every((x, i) => i === 0 || v[i - 1] <= x), "default sort: cheapest follower first " + JSON.stringify(v));
await page.click('#ads-table th[data-sort="cpf"]');
v = await cpfs();
ok(v.every((x, i) => i === 0 || v[i - 1] >= x), "click header: sorted descending " + JSON.stringify(v));
await page.click('#ads-table th[data-sort="spent"]');
const sp = (await page.locator("#ads-table tbody td.spent").allInnerTexts()).map(t => parseFloat(t.replace(/,/g, ""))).filter(n => !isNaN(n));
ok(sp.every((x, i) => i === 0 || sp[i - 1] <= x), "sort by spent ascending " + JSON.stringify(sp));

// --- chart
await page.click('nav button[data-tab="overview"]');
ok(await page.locator('#chart path.series[data-p="tiktok"]').count() === 1, "chart: TikTok line");
ok(await page.locator('#chart path.series[data-p="facebook"]').count() === 1, "chart: Facebook line");
ok((await txt(page, "#chart-note")).includes("2 سجل"), "chart note lists the 2 undated records");

// --- CSV: Meta
await page.click('nav button[data-tab="csv"]');
await page.fill("#csv-text", META_CSV);
await page.click("#csv-read");
const mapped = async f => page.locator(`#csv-map-table select[data-field="${f}"] option:checked`).innerText();
ok(await mapped("spent") === "Amount spent (EGP)", "Meta CSV: spent → Amount spent (EGP)");
ok(await mapped("views") === "ThruPlays", "Meta CSV: views → ThruPlays");
ok(await mapped("visits") === "Instagram profile visits", "Meta CSV: visits → Instagram profile visits");
ok(await mapped("followers") === "Instagram follows", "Meta CSV: followers → Instagram follows");
ok(await mapped("saves") === "Post saves" && await mapped("shares") === "Post shares", "Meta CSV: saves/shares mapped");
ok(await mapped("date") === "Reporting starts" && await mapped("audience") === "Ad set name", "Meta CSV: date/audience mapped");
ok((await txt(page, "#csv-count")).startsWith("2 "), "Meta CSV: 2 rows (totals row skipped)");
await page.screenshot({ path: path.join(OUT, "3-csv-map.png"), fullPage: true });
await page.click("#csv-import");
ok(await rows() === 7, "Meta CSV imported");
const st = await page.evaluate(k => JSON.parse(localStorage.getItem(k)).rows, "wasem-ads-dashboard-v1");
const m1 = st.find(r => r.note === "من CSV" && r.platform === "instagram");
ok(m1 && m1.spent === 100.5 && m1.views === 4100 && m1.visits === 61 && m1.followers === 9 && m1.date === "2026-10-24", "Meta row values parsed (100.50, 4,100, 61, 9)");
ok(st.some(r => r.note === "من CSV" && r.platform === "facebook"), "Meta CSV: Platform column → Facebook row");

// --- CSV: TikTok (file upload)
await page.click('nav button[data-tab="csv"]');
const tf = path.join(OUT, "tiktok.csv");
fs.writeFileSync(tf, TIKTOK_CSV);
await page.setInputFiles("#csv-file", tf);
await page.waitForSelector("#csv-map", { state: "visible" });
ok(await page.locator("#csv-platform").inputValue() === "tiktok", "TikTok CSV: platform detected");
ok(await mapped("spent") === "Cost" && await mapped("views") === "Video views" && await mapped("followers") === "Paid followers", "TikTok CSV: Cost / Video views / Paid followers");
ok(await mapped("visits") === "Profile visits", "TikTok CSV: Profile visits (not the rate)");
await page.click("#csv-import");
ok(await rows() === 8, "TikTok CSV imported");

// --- reels
await page.click('nav button[data-tab="reels"]');
const reels = await page.locator("#reels-table tbody tr").allInnerTexts();
ok(reels.some(r => r.includes("تعبت أشرح")) && reels.some(r => r.includes("ريل الاختبار")) && reels.some(r => r.includes("من غير ريل")), "reels table groups by reel");
ok(await page.locator("#reels-table tbody tr.best").count() === 1, "reels table: best reel highlighted");
await page.screenshot({ path: path.join(OUT, "4-reels.png"), fullPage: true });

// --- persistence
await page.reload();
ok(await page.locator("#records-table tbody tr").count() === 8, "after reload: 8 records still there");

// --- export / clear / import
const [dl] = await Promise.all([page.waitForEvent("download"), page.click("#btn-export")]);
const jf = path.join(OUT, "backup.json");
await dl.saveAs(jf);
const backup = JSON.parse(fs.readFileSync(jf, "utf8"));
ok(backup.rows.length === 8 && /^ads-dashboard-backup-\d{4}-\d{2}-\d{2}\.json$/.test(dl.suggestedFilename()), "Export JSON: " + dl.suggestedFilename());
await page.click('nav button[data-tab="records"]');
await page.click("#btn-clear");
ok(await rows() === 0, "clear all");
await page.setInputFiles("#json-file", jf);
await page.waitForFunction(() => document.querySelectorAll("#records-table tbody tr[data-id]").length === 8);
ok(await rows() === 8, "Import JSON restores all 8 records");
await page.click('nav button[data-tab="overview"]');
await page.screenshot({ path: path.join(OUT, "5-overview-after.png"), fullPage: true });

// --- edit + delete
await page.click('nav button[data-tab="records"]');
await page.locator("#records-table tbody tr", { hasText: "اختبار فيسبوك" }).locator("[data-edit]").click();
await page.fill('#form [name="followers"]', "25");
await page.click("#form-submit");
await page.click('nav button[data-tab="ads"]');
ok((await adRow("اختبار فيسبوك").locator("td.cpf").innerText()) === "2.00", "edit: CPF updated to 2.00");
await page.click('nav button[data-tab="records"]');
await page.locator("#records-table tbody tr", { hasText: "اختبار فيسبوك" }).locator("[data-del]").click();
ok(await rows() === 7, "delete a record");

// --- phone
const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
const pp = await phone.newPage();
await pp.goto(url);
await pp.evaluate(() => document.fonts.ready);
const overflow = await pp.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
ok(overflow <= 1, "phone: no horizontal page scroll (" + overflow + "px)");
await pp.screenshot({ path: path.join(OUT, "6-phone.png"), fullPage: true });

ok(errors.length === 0, "no JavaScript errors " + (errors.length ? JSON.stringify(errors) : ""));
await browser.close();
console.log(failures ? `\n${failures} FAILED` : "\nALL GOOD");
process.exit(failures ? 1 : 0);

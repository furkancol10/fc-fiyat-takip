# Araştırma raporu (Hydrogen, doğrulanmamış veri)

## 1. Veri Kaynağı, Hukuk ve Demo Stratejisi
- **Çekme yolları:**
  - *JSON-LD (`schema.org`):* `<script type="application/ld+json">` içindeki `Product` / `Offer` (`price`, `priceCurrency`, `availability`: `InStock`/`OutOfStock`) CSS değişikliklerine karşı en dayanıklı kaynaktır ([Schema.org](https://schema.org/Product)).
  - *HTML scraping:* JSON-LD yoksa CSS seçicilerle okuma; sayfa yapısı değişince kırılır.
  - *Pazaryeri sayfaları:* WAF/Cloudflare ve bot koruması nedeniyle ücretsiz sunucudan düzenli çekimde engelleme riski yüksektir.
  - *Resmi API:* Amazon SP-API (`sellingpartnerapi-eu.amazon.com`), Trendyol ve Hepsiburada API'leri satıcının **kendi mağaza** fiyat/stok yönetimi içindir; rakip verisine açık uç sunup sunmadığı her platform için ayrıca doğrulanmadı.
- **Hukuki/etik risk:** `robots.txt` ve kullanım şartları ihlali erişim yasağına yol açabilir (`rfc9309` resmî URL'si burada doğrulanmadı). *KVKK (6698):* Ürün fiyatı/stoku kişisel veri değildir, ancak satıcı adı/yorum/soru-cevap çekilirse KVKK kapsamına girer; toplu kazıma FSEK Ek m. 8 ve TTK m. 55 riski doğurur (resmî mevzuat URL'leri burada doğrulanmadı).
- **Demo ve müşteri anlatımı:**
  - *Portföy demosu:* Gerçek siteler yerine uygulamanın kendi sunduğu **sahte rakip mağaza** sayfaları (JSON-LD + HTML) taranmalı.
  - *Müşteriye anlatım:* "Kendi mağazanız için resmi API; rakipler için yalnızca müşterinin onayladığı URL'lerde herkese açık fiyat/stok alanı, `robots.txt` uyumu ve düşük istek sıklığı."

## 2. Telegram Bot API
- **Kurulum ve `chat_id`:** `@BotFather` üzerinden `/newbot` ile token alınır. Kullanıcı bota ilk mesajı attıktan sonra `getUpdates` JSON yanıtındaki `chat.id` okunur ([Telegram Bot API](https://core.telegram.org/bots/api#sendmessage)).
- **Uyarı ve `sendMessage`:** `POST https://api.telegram.org/bot<TOKEN>/sendMessage` (`chat_id`, `text`) ile; sabit eşik altı, `%X` düşüş ve stok değişimi (`OutOfStock` ↔ `InStock`) uyarısı atılır.
- **Sınırlar ve gürültü azaltma:** Tek sohbete ~1 mesaj/sn, gruba 20 mesaj/dk, genel ~30 mesaj/sn; aşılınca `429` + `retry_after` döner. Yalnızca durum/fiyat değişiminde gönderim, kural başına `cooldown` ve minimum fark eşiği gürültüyü azaltır.

## 3. Zamanlama ve Gece Sıfırlama
- **GitHub Actions `schedule`:** Ücretsiz; en küçük aralık 5 dk, yoğun saatlerde 10–45 dk gecikebilir/atlanabilir, 60 gün repo hareketsizliğinde durur ([GitHub Docs](https://docs.github.com/en/actions/writing-workflows/choosing-when-your-workflow-runs/events-that-trigger-workflows)).
- **Vercel Cron (Hobby):** Günde en fazla **1 kez** (1 saatlik esnek pencerede) çalışır ([Vercel Docs](https://vercel.com/docs/cron-jobs)).
- **Uygulama içi scheduler (APScheduler):** Tek süreçte kolaydır ([APScheduler](https://apscheduler.readthedocs.io/)), fakat Render Free (15 dk boşta uyur, 750 saat/ay) gibi uyuyan barındırmada süreç uyuyunca çalışmaz ([Render Docs](https://render.com/docs/free)).
- **Sistem `cron`:** En kararlı ama VPS maliyeti gerektirir.
- **Demo uyumu:** Gece tek seferlik reset+seed (`0 0 * * *` UTC) için GitHub Actions veya Vercel Hobby uyumludur. Periyodik kontrol için GitHub Actions ile `POST /jobs/check` tetiklemesi + panelde **"Şimdi Kontrol Et"** butonu önerilir.

## 4. Python Panel Çerçevesi ve Ücretsiz PostgreSQL
- **FastAPI + Jinja2 (önerilen):** Tek uygulamada SSR panel + cron webhook uçları sunar ([FastAPI Templates](https://fastapi.tiangolo.com/tutorial/templates/)).
- **Flask:** Async gerekmiyorsa eşit derecede sade alternatif ([Flask](https://flask.palletsprojects.com/)).
- **Streamlit:** Cron/webhook ucu sunmaz, soğuk başlangıcı ağırdır ([Streamlit](https://docs.streamlit.io/)).
- **Postgres erişimi:** `psycopg` (v3) ile doğrudan parametreli SQL ([Psycopg 3](https://www.psycopg.org/psycopg3/docs/)).
- **Ücretsiz Postgres:**
  - *Neon (önerilen):* 1 GB depolama/proje, 100 CU-saat/ay, 5 dk boşta scale-to-zero, 5 GB egress ([Neon Docs](https://neon.tech/docs)).
  - *Supabase:* 500 MB DB, 5 GB egress, maks. 2 aktif proje, **7 gün düşük aktivitede pause**, otomatik yedek yok ([Supabase Docs](https://supabase.com/docs)).

## 5. Şema ve En Küçük 2 Günlük Mimari
- **Tablolar:**
  - `products (id, sku, name, my_price, created_at)`
  - `competitors (id, product_id, store_name, url, parse_mode, last_price, in_stock, last_checked_at)`
  - `price_history (id, competitor_id, price, in_stock, checked_at)`
  - `alert_rules (id, competitor_id, rule_type, threshold_value, cooldown_min, last_sent_at, is_active)`
- **En küçük mimari:** Tek repo **FastAPI + Jinja2 + psycopg3** (Render Free) + **Neon PostgreSQL** + uygulama içi `/mock-store/{slug}` sahte mağaza (JSON-LD + HTML) + **GitHub Actions** (gece reset + periyodik kontrol) + **Telegram `sendMessage`**.

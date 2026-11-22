# Mimari
Durum: onaylı (kullanıcı, 2026-11-22)
Kaynak: reports/03-mimari-taslak-neon.md (tam taslak), reports/01-02 (araştırma + Argon doğrulaması).

## Kararlar
- **Uygulama:** tek monolit FastAPI + Jinja2 + psycopg3 (parametreli SQL, ORM yok). Flask/Streamlit elendi (Streamlit cron/webhook ucu sunmaz).
- **Veritabanı:** PostgreSQL (ücretsiz Neon). Supabase 7 gün pause yüzünden elendi.
- **Modüller:** Panel (SSR), Sahte Mağaza `/mock-store/{slug}`, Çekici+Parser (`httpx`), Kural/Telegram motoru, Depo katmanı.
- **Veri kaynağı:** yalnız URL (MVP). `source_type`/resmi API (Amazon SP-API vb.) kapsam dışı. Demo yalnız kendi sahte mağazamızı tarar; gerçek pazaryeri kazıması yok; LLM yok.
- **Telegram:** halka açık demoda varsayılan `dry_run` (mesaj panelde önizlenir/`alert_logs`'a yazılır, gerçek gönderim yok). Gerçek gönderim yalnız token tanımlı müşteri kurulumunda. `429` → `retry_after` kadar bekle, 1 yeniden deneme.
- **Parser:** önce JSON-LD (`Product.offers` altında `Offer`: price, priceCurrency, availability; tam URI `https://schema.org/InStock` ve kısa biçim normalize), yoksa CSS seçici. İstek 5 sn zaman aşımı, 5xx/bağlantıda en çok 2 yeniden deneme, 4xx/parse hatasında yok. Bir rakibin hatası turu durdurmaz; son geçerli fiyat korunur.
- **Tetikleme:** uygulama içi scheduler yok. Dış zamanlayıcı (GitHub Actions/cron; kurulum kullanıcıda) `POST /jobs/check` ve `POST /jobs/reset-demo` çağırır; panelde "Şimdi Kontrol Et". `pg_try_advisory_lock` ile tur çakışması engellenir.
- **Uyarı kuralları:** `below_price`, `pct_drop`, `stock_change`. İlk ölçüm baz (uyarı yok), yalnız değişimde, min fark eşiği, kural başına `cooldown_min` (varsayılan 60).
- **Güvenlik:** demoda yalnız kendi `/mock-store/*` yolları izinli, yönlendirme kapalı, özel/yerel IP engeli; `/jobs/*` `X-Job-Token` (sabit zamanlı karşılaştırma); kota maks. 10 ürün/20 rakip; tur başına 30 sn soğuma; token'lar `.env`.

## Şema
`products`, `competitors` (UNIQUE product_id+url; son fiyat/stok, consecutive_failures, parse_mode, css seçiciler), `price_history` (indeks competitor_id, checked_at DESC), `alert_rules`, `alert_logs` (status sent|failed|dry_run), `mock_store_items` (render_mode jsonld|css_only|broken). Sütun ayrıntısı: taslak §3.

## Test
Gerçek PostgreSQL test şeması + FastAPI `TestClient`, dış ağ yok, Telegram istemcisi sınırda sahte. Kabul: JSON-LD/CSS/bozuk HTML; uyarı + cooldown; izin dışı URL 400, token 401, hız 429; `reset-demo` deterministik; `dry_run` gerçek gönderim yapmaz.

## Açık riskler
Soğuk başlangıç (dış cron zaman aşımı ≥30 sn); public repoda GitHub Actions 60 gün hareketsizlikte durur; barındırma ve cron kurulumu kullanıcıda.

Referans: [01-arastirma-hydrogen.md](file:///home/pcf/Desktop/projeler/freelance/portfolyo/fc-fiyat-takip/reports/01-arastirma-hydrogen.md), [02-argon-dogrulama.md](file:///home/pcf/Desktop/projeler/freelance/portfolyo/fc-fiyat-takip/reports/02-argon-dogrulama.md).

### 1) Bileşenler ve sınırlar
- **Tek monolit web uygulaması (FastAPI + Jinja2 + psycopg3)** ve **Neon PostgreSQL** (`neon.com`). Gerekçe: Tek geliştirici ve ~2 günlük sürede sıfır ek altyapı yükü; Neon ücretsiz katmanda 5 dk scale-to-zero sunar ve duraklatılmaz. *Alternatif:* Flask benzer sadeliktedir ancak FastAPI yerleşik doğrulama verir; Supabase 7 gün düşük aktivitede durduğu ve ORM (SQLAlchemy) bu ölçekte gereksiz soyutlama eklediği için elendi.
- **Beş iç modül:** Panel (SSR UI), Sahte Mağaza (`/mock-store/{slug}`), Tarayıcı/Parser (`httpx`), Kural/Telegram Motoru, Depo Katmanı (parametreli SQL). Demo yalnızca kendi sahte mağazamızı tarar; gerçek pazaryeri kazıması ve LLM yoktur.

### 2) Veri akışı (kontrol turu, değişim tespiti, uyarı)
- Dış zamanlayıcı (`POST /jobs/check`) veya paneldeki "Şimdi Kontrol Et" butonu turu başlatır; PostgreSQL danışma kilidi (`pg_try_advisory_lock`) eşzamanlı turların çakışmasını önler.
- Aktif rakipler okunur; her URL izin listesinden geçirilip çekilir, fiyat (`NUMERIC`), para birimi ve stok durumu (`in_stock`) çıkarılır.
- Başarılı ölçüm `price_history`'ye eklenir, önceki `last_price`/`last_in_stock` ile karşılaştırılıp güncellenir; başarısız çekimde son geçerli fiyat korunur.
- Gerçek değişimde ilgili aktif `alert_rules` değerlendirilir; koşul ve soğuma süresi uygunsa Telegram mesajı gönderilir, sonuç `alert_logs`'a işlenir.

### 3) Şema (tablolar, anahtarlar, indeksler)
- **products:** `id` (PK), `sku` (UNIQUE), `name`, `my_price` (`NUMERIC(12,2)`), `currency`, `created_at`.
- **competitors:** `id` (PK), `product_id` (FK CASCADE), `store_name`, `url`, `parse_mode` (`auto|jsonld|css`), `css_price_selector`, `css_stock_selector`, `last_price`, `last_in_stock`, `last_status`, `consecutive_failures`, `last_checked_at`, `is_active`; UNIQUE `(product_id, url)`.
- **price_history:** `id` (PK), `competitor_id` (FK CASCADE), `price`, `in_stock`, `checked_at`; indeks `(competitor_id, checked_at DESC)`.
- **alert_rules:** `id` (PK), `competitor_id` (FK CASCADE), `rule_type` (`below_price|pct_drop|stock_change`), `threshold_value`, `cooldown_min`, `last_sent_at`, `is_active`; kısmi indeks `(competitor_id)` WHERE `is_active`.
- **alert_logs:** `id` (PK), `rule_id` (FK CASCADE), `event_type`, `old_val`, `new_val`, `status` (`sent|failed|dry_run`), `created_at`; indeks `(created_at DESC)`.
- **mock_store_items:** `slug` (PK), `name`, `price`, `currency`, `in_stock`, `render_mode` (`jsonld|css_only|broken`), `updated_at`.
- Gerekçe: `competitors` üzerindeki son değerler hızlı karşılaştırma, `mock_store_items` ise gece sıfırlanabilir canlı demo sağlar. *Alternatif:* Yalnız `price_history` tutup son değeri sorgulamak tabloyu küçültür ama her turda pencere sorgusu maliyeti doğurur.

### 4) Parser stratejisi (JSON-LD önce, CSS fallback), hata/yeniden deneme
- Önce `application/ld+json` bloğunda `Product.offers` altındaki `Offer` nesnesinden `price`, `priceCurrency` ve `availability` okunur. Argon düzeltmesine uygun olarak sahte mağaza tam URI (`https://schema.org/InStock`, `https://schema.org/OutOfStock`) üretir; parser tam URI ve kısa biçimleri normalize eder. JSON-LD yoksa veya `parse_mode=css` ise CSS seçicilere düşülür.
- Gerekçe: Yapısal veri (JSON-LD) arayüz değişikliklerine dayanıklıdır. *Alternatif:* Playwright JS ağırlıklı sayfaları açar ancak ücretsiz sunucuda bellek ve süre sınırını aşar.
- Hata yönetimi: İstek başına 5 sn zaman aşımı; bağlantı/5xx hatalarında üstel beklemeyle en fazla 2 yeniden deneme, 4xx/parse hatasında yeniden deneme yapılmaz. Bir rakip hatası turu durdurmaz, `consecutive_failures` artar.

### 5) Uyarı kuralları ve gürültü azaltma
- **Kurallar:** Sabit eşik altı (`below_price`), önceki geçerli fiyata göre `%X` düşüş (`pct_drop`) ve stok geçişi (`stock_change`: var↔yok).
- **Gürültü azaltma:** İlk başarılı ölçüm baz kabul edilir (uyarı gönderilmez); yalnızca değişimde tetiklenir, minimum fark eşiği ve kural başına `cooldown_min` (varsayılan 60 dk) uygulanır.
- Telegram `sendMessage` tek sohbete ≤1 mesaj/sn hızında sıralı atılır; HTTP `429` yanıtında `parameters.retry_after` kadar beklenip bir kez daha denenir. *Alternatif:* Redis/Celery kuyruğu yüksek hacimde gerekir ama mikro satıcı ölçeğinde gereksiz karmaşıklık ekler.

### 6) Zamanlama/tetikleme, demo sıfırlama
- Render Free 15 dk hareketsizlikte uyuduğu için süreç içi APScheduler kullanılmaz. Periyodik kontrol (`POST /jobs/check`) ve gece sıfırlama (`POST /jobs/reset-demo`) kullanıcının kuracağı dış zamanlayıcıdan (GitHub Actions `schedule` veya sistem cron) tetiklenir; panelde anlık deneme için "Şimdi Kontrol Et" bulunur.
- `reset-demo` tek işlemde demo tablolarını temizleyip sabit tohum verisini (JSON-LD, CSS-only ve bozuk sayfa senaryolarıyla) geri yükler.
- *Alternatif:* Vercel Cron Hobby günde yalnızca 1 kez çalıştığından periyodik taramaya uymaz, yalnız gece sıfırlama için uygundur.

### 7) Güvenlik/kötüye kullanım (SSRF, URL izin listesi, hız sınırı, token)
- **SSRF ve izin listesi:** Demo modunda yalnızca kendi alan adımızın `/mock-store/*` yollarına izin verilir; HTTP yönlendirmesi kapatılır, özel/yerel IP'lere dış istek engellenir. Müşteri kurulumunda izinli alan adı listesi ve `robots.txt` (RFC 9309) uyumu, KVKK/FSEK/TTK riskine karşı yalnız herkese açık fiyat/stok alanı okunur.
- **Token ve hız sınırı:** `/jobs/*` uçları sabit zamanlı karşılaştırılan `X-Job-Token` ile korunur. Halka açık demoda kayıt kotası (maks. 10 ürün, 20 rakip) ve bellek içi hız sınırı (tur başına 30 sn soğuma) uygulanır. *Alternatif:* Redis tabanlı hız sınırı çoklu kopyada gerekir, tek süreçte fazladır.

### 8) Test stratejisi (kabul testleri)
- İzole PostgreSQL test şeması ve FastAPI `TestClient` ile uçtan uca kabul testleri yazılır; dış ağ çağrısı yapılmaz, Telegram istemcisi sınırda sahte 200 ve `429 + retry_after` yanıtlarıyla sınanır.
- Kabul senaryoları: (1) JSON-LD `Product.offers` tam URI okuma, CSS yedeğine düşme, bozuk HTML'de eski fiyatı koruma; (2) eşik/% düşüş/stok değişiminde uyarı, aynı değerde ve `cooldown` içinde tekrar atılmaması; (3) izin dışı URL'de 400, geçersiz `X-Job-Token`'da 401, hız aşımında 429; (4) `reset-demo` sonrası deterministik tohum durumu.
- *Alternatif:* Yalnız mock birim testleri hızlıdır ama SQL ve HTTP sınır hatalarını kaçırır.

### 9) 2 günlük iş kırılımı
- **Gün 1:** PostgreSQL şeması + tohum verisi; `/mock-store/{slug}` (JSON-LD, CSS-only, broken); URL izin listesi, `httpx` çekici ve JSON-LD/CSS parser; çekirdek kabul testleri.
- **Gün 2:** Danışma kilitli kontrol turu, değişim tespiti, kural/soğuma ve Telegram `sendMessage` (429 uyumlu); Jinja2 panel + "Şimdi Kontrol Et" + demo fiyat/stok değiştirme aracı; `/jobs/check`, `/jobs/reset-demo`, token/hız sınırı, tüm kabul testlerinin geçişi ve cron/dağıtım notu.

### 10) Riskler ve açık sorular
- **Riskler:** Render Free + Neon scale-to-zero soğuk başlangıçta gecikme yaratabilir (dış cron zaman aşımı ≥30 sn olmalı); public repoda GitHub Actions `schedule` 60 gün hareketsizlikte durur; halka açık demoda gerçek Telegram botu spam riski taşır.
- **Açık sorular (onay gerekli):**
  1. Halka açık demoda Telegram bildirimi varsayılan `dry_run` (panelde mesaj önizlemesi/log) mu olsun, yoksa sıkı hız sınırlı tek bir demo kanalına mı gitsin?
  2. İlerideki müşteri uyarlamaları için Amazon SP-API (`Product Pricing API`) gibi resmi kaynaklara şimdiden `source_type` sütunuyla yer açalım mı, yoksa MVP yalnız URL mi kalsın?


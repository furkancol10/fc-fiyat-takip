# fc-fiyat-takip

## Ne yapar

`fc-fiyat-takip`, mikro e-ticaret satıcıları için rakip ürünlerin fiyat ve stok durumlarını URL üzerinden takip eden FastAPI, Jinja2 ve PostgreSQL (`psycopg3`) tabanlı bir web uygulamasıdır. Tanımlı kurallara (`below_price`, `pct_drop`, `stock_change`) göre değişimde uyarı üretir, geçmişi kaydeder ve sade bir web paneli sunar. Demo sürüm yalnızca yerleşik sahte mağazayı (`/mock-store/{slug}`) tarar ve Telegram bildirimlerini varsayılan olarak `dry_run` modunda kaydeder.

## Demo akışı

`http://127.0.0.1:8000/` açıldığında tohum verisi (3 ürün, 6 rakip, 3 kural) hazır gelir:

1. **Baz kontrol:** Panelde **Şimdi Kontrol Et** düğmesine basın (`POST /panel/check`). İlk ölçüm baz olur; uyarı üretilmez (bozuk şablonlu 2 rakip hata olarak kaydedilir, turu durdurmaz).
2. **Sahte mağazadan fiyat düşür:** **Sahte Mağazayı Değiştir (Demo Aracı)** tablosundan örneğin Kahve Diyarı fiyatını `2400.00` altına (örn. `2350.00`) indirip **Güncelle** deyin.
3. **Tekrar kontrol:** 30 saniyelik soğuma süresi sonrası yeniden **Şimdi Kontrol Et** düğmesine basın.
4. **`dry_run` uyarısı:** Değişim algılanır ve **Son 20 Uyarı** tablosunda `dry_run` olarak görünür (gerçek Telegram gönderimi yapılmaz).

## Kurulum

```bash
docker compose up -d
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL="postgresql://fiyattakip:fiyattakip@localhost:5435/fiyattakip"
python -m app.setup
uvicorn app.main:app --reload
```

`python -m app.setup` şemayı kurup boşsa tohum verisini yükler, kuruluysa dokunmaz; sıfırlamak için `python -m app.setup --reset` kullanılır. Uygulama `http://127.0.0.1:8000` adresinde açılır; farklı bir port kullanırsanız `MOCK_BASE_URL` değerini de aynı porta çevirin (kontrol turu sahte mağazayı bu adresten çeker).

| Değişken | Varsayılan | Açıklama |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://fiyattakip:fiyattakip@localhost:5435/fiyattakip` | PostgreSQL adresi (`app.setup` için zorunlu) |
| `JOB_TOKEN` | — | `/jobs/*` için `X-Job-Token` (boşsa `503`) |
| `MOCK_BASE_URL` | `http://127.0.0.1:8000` | Sahte mağaza taban adresi |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot anahtarı (boşsa `dry_run`) |
| `TELEGRAM_CHAT_ID` | — | Hedef sohbet kimliği (boşsa `dry_run`) |
| `TELEGRAM_DRY_RUN` | `true` | Yalnız `"false"` ise gerçek gönderim açılır |
| `TRUST_PROXY` | `0` | `"1"` ise hız sınırında `X-Forwarded-For` ilk IP alınır |

## Testler

PostgreSQL (`docker compose up -d`) çalışırken:

```bash
pytest
```

223 test vardır, temiz kopyada geçer. Testler yalıtılmış PostgreSQL şemalarında (`fiyattakip_test`, `fiyattakip_setup_test`) dış ağsız çalışır; veritabanını `TEST_BASE_DATABASE_URL` ile değiştirebilirsiniz (varsayılan: Compose'daki `fiyattakip` veritabanı).

## Zamanlama

Uygulama içi zamanlayıcı yoktur; dağıtım ve cron kurulumu kullanıcıya aittir. Dış zamanlayıcı `/jobs/check` ve `/jobs/reset-demo` uçlarını `X-Job-Token` başlığıyla tetikler:

```bash
curl -X POST http://127.0.0.1:8000/jobs/check -H "X-Job-Token: $JOB_TOKEN"
curl -X POST http://127.0.0.1:8000/jobs/reset-demo -H "X-Job-Token: $JOB_TOKEN"
```

`/jobs/check` `pg_try_advisory_lock` ile çakışan turları atlar (`skipped: true`), 30 sn soğuma uygular (`429`, `Retry-After` başlığıyla; panel düğmesinde aynı soğuma bir iletiyle bildirilir). `/jobs/reset-demo` tabloları tohuma döndürür.

## Mimari özet

- **Modüller:** Monolit FastAPI + Jinja2 + `psycopg3` (ORM yok, parametreli SQL). `main.py` (`/healthz`, `/mock-store/{slug}`, güvenlik başlıkları), `panel.py`, `jobs.py`, `checker.py`, `fetcher.py` (`httpx`, 5 sn zaman aşımı, maks 1 MB, 5xx/ağda 2 yeniden deneme), `parser.py`, `rules.py`, `telegram.py` (429'da `retry_after` ile 1 deneme), `security.py`, `limits.py`, `db.py`, `seed.py`, `reset.py`, `setup.py`.
- **Şema (`db/schema.sql`, 6 tablo):** `products`, `competitors`, `price_history`, `alert_rules`, `alert_logs`, `mock_store_items`.
- **Parser:** `auto` modunda önce JSON-LD (`Product` / `Offer`), yoksa CSS seçicileri (varsayılan `.price`, `.stock`) yedek olarak çalışır; `jsonld` ve `css` doğrudan seçilebilir.

## Güvenlik

- **SSRF:** Demoda katı host+port ve `/mock-store/` önek izni, yönlendirme kapalı; katı olmayan modda özel/yerel IP'ler engellenir.
- **CSRF ve başlıklar:** Panel `POST` formlarında çerez/form `csrf_token` eşleşmesi; `CSP`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.
- **Hız sınırı ve kota:** Panel `POST` için IP başına 60 sn'de 30 istek; kontrol turunda global 30 sn soğuma; maks 10 ürün, 20 rakip, 64 KB form gövdesi.
- **Token ve `dry_run`:** `/jobs/*` `hmac.compare_digest` ile `X-Job-Token` doğrular (`503`/`401`). Telegram varsayılan `dry_run` modundadır; bot anahtarı loglardan maskelenir.

## Bilinen sınırlar

- **DNS rebinding:** Müşteri kurulumunda (katı olmayan politikada) çözümlenen IP bağlantıya sabitlenmez.
- **Kota yarışı:** Eşzamanlı isteklerde ürün/rakip kotası birkaç kayıt aşılabilir.
- **İmzasız CSRF çerezi:** `csrf_token` çerezi imzasızdır.
- **Mükerrer uyarı:** Telegram gönderimi ile kayıt yazımı arasında çökme olursa uyarı tekrarlanabilir.
- **Kendi sunucusuna HTTP isteği:** Kontrol turu sahte mağazayı `MOCK_BASE_URL` üzerinden kendi sunucusuna HTTP isteği atarak çeker.

## Hukuki not

Demo yalnızca kendi sahte mağazasını (`/mock-store/*`) tarar. Gerçek sitelerde kullanım hâlinde `robots.txt`, kullanım şartları ve KVKK uyumu müşterinin sorumluluğundadır.


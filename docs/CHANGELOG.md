# Changelog
- 2026-11-22: proje iskeleti kuruldu
- 2026-11-22: Hydrogen araştırması, Argon doğrulaması, Neon mimari taslağı; mimari onaylandı (dry_run, yalnız URL)
- 2026-11-22: Gün 1a (şema, tohum, sahte mağaza) ve Gün 1b (SSRF, çekici, parser; katı demo host, TR öncelikli fiyat) tamamlandı, 147 test
- 2026-11-22: Gün 2a (kontrol turu, uyarı kuralları, Telegram dry_run) tamamlandı, 188 test. Bilinen riskler: alert_logs yazımı hata yönetimi dışı; çökme anında mükerrer uyarı olabilir
- 2026-11-23: Gün 2b (panel, /jobs uçları, CSRF, kota, hız sınırı, python -m app.setup) tamamlandı. Bilinen riskler: kota yarışı, imzasız CSRF çerezi
- 2026-11-23: README ve vaka metni (docs/VAKA_METNI.md) tamamlandı; temiz kopyada kurulum doğrulandı

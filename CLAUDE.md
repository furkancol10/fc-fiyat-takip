# fc-fiyat-takip
## Amaç
Mikro e-ticaret satıcısı için rakip fiyat/stok takibi; değişimde Telegram uyarısı ve basit panel (Bionluk portföy projesi 5).
## Stack
Python, FastAPI + Jinja2 + psycopg3, PostgreSQL (Neon). LLM yok. Mimari: docs/ARCHITECTURE.md (onaylı).
## Komutlar
- test: pytest (kurulumdan sonra doğrulanacak)
- çalıştır: uvicorn app.main:app --reload (kurulumdan sonra doğrulanacak)
## Model rolleri
Claude = planlar, kod yazar, commit eder; Gem Neon = kod/plan review ve ikinci görüş; Hydrogen = araştırma özetleri (reports/). Ayrıntı: kullanıcının global ~/.claude/CLAUDE.md dosyası.
## Kurallar
- Kuralları yalnızca CLAUDE.md'de düzenle (AGENTS.md/GEMINI.md symlink).
- Mimari karar onaylanıp docs/ARCHITECTURE.md'ye yazılmadan uygulama kodu yazılmaz.
- Önce araştırma: docs/NOTES.md ve reports/ dolar, sonra mimari kullanıcıya onaylatılır.
- Test: yeni davranış ve hata düzeltmesinde testi uygulamadan önce çalıştır ve FAIL gör; yalnız-mock ya da totolojik test yazma.
- Her görevin bir "bitti ne zaman" satırı olur.
- Kritik iş başka bir ajan/model tarafından gözden geçirilir.
- CLAUDE.md kısa kalır (≤40 satır); ayrıntı docs/ altına gider.
- Push'u yalnızca kullanıcı yapar; deploy/cron adımları kullanıcıya aittir.
- Portföyde Visbody markası/verisi kullanılmaz; LLM kullanılmaz.
## Bitti tanımı
Görevin "bitti ne zaman" koşulu doğrulandı (kodda kabul testi geçer), kritik iş başka ajan/model tarafından gözden geçirildi, madde BACKLOG'dan CHANGELOG'a taşındı.
## Bağlantılar
- docs/NOTES.md (ürün kırılımı) | docs/ARCHITECTURE.md (mimari) | docs/BACKLOG.md (iş listesi) | docs/CHANGELOG.md (biten işler)
- docs/CONSTANTS.md (sabitler: endpoint, adres, sürüm) | reports/ (araştırma çıktıları)

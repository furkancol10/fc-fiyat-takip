# Vaka metni (Bionluk portföy)

## Sorun

Küçük bir e-ticaret mağazasında rakiplerin fiyat ve stok durumunu tek tek açıp bakmak zaman alır. Bir rakip indirim yaptığında ya da ürünü tükendiğinde bunu geç fark etmek, fiyat kararlarını geciktirir. Çoğu satıcının ihtiyacı karmaşık bir araç değil: takip ettiği ürünleri sade bir ekranda görmek ve değişim olunca haberdar olmak.

## Çözüm

Ürünlerin ve rakip sayfa adreslerinin panelden eklendiği, kural tabanlı bir fiyat ve stok takip uygulaması yaptım (FastAPI, PostgreSQL). Üç kural türü var: hedef fiyatın altına düşme, yüzde olarak indirim ve stok değişimi. Her kuralın kendi bekleme süresi vardır, bu yüzden aynı uyarı arka arkaya tekrarlanmaz. İlk ölçüm baz alınır, uyarı yalnızca sonraki değişimlerde üretilir. Bir rakip sayfası bozuk çıkarsa o rakip hata olarak kaydedilir, kontrol turu devam eder ve son geçerli fiyat korunur.

Yapay zekâ kullanılmıyor; her şey açık kurallarla çalışıyor. Canlı demo gerçek mağazaları taramaz, yalnızca kendi içindeki sahte mağazayı okur. Telegram uyarıları demoda gönderilmez, panelde kayıt olarak görünür (`dry_run`). Bot anahtarı tanımlı bir kurulumda ise mesaj gerçekten Telegram'a gider.

## Sonuç

- Panelden "Şimdi Kontrol Et" ile tur başlatılır, dış bir zamanlayıcı da aynı işi anahtarlı bir uçtan tetikleyebilir.
- Her rakip için fiyat geçmişi grafiği ve son uyarılar panelde görünür.
- Sahte mağazadan fiyat veya stok değiştirip uyarının oluşmasını canlı izleyebilirsiniz.
- Kayıt sınırı 10 ürün ve 20 rakip; panel formları CSRF korumalı, istekler IP başına hız sınırlı.
- 223 otomatik test temiz kopyada geçiyor.

## Size nasıl uyarlarım?

Takip etmek istediğiniz ürünleri ve izin verdiğiniz rakip sayfalarını paylaşırsanız kuralları mağazanıza göre kurarım ve uyarıları Telegram'a bağlarım. Önce canlı demoyu birlikte inceleyip ihtiyacı netleştirebiliriz. Gerçek sitelerde kullanım için robots.txt, site koşulları ve KVKK uyumu sizin onayınızla ilerler.

---

## Bionluk teklif metni (≤600 karakter)

Merhaba, mağazanızdaki ürünler için rakip fiyat ve stok değişimini düzenli kontrol eden, değişimde Telegram'dan haber veren sade bir takip paneli kurabilirim. Hedef fiyat, yüzde indirim ve stok değişimi için kural belirlersiniz; her rakip için fiyat geçmişi grafiğini görürsünüz. Yapay zekâ yok, açık kurallarla çalışır; yalnızca sizin onayladığınız herkese açık sayfalar izlenir. Kendi sahte mağazasını tarayan demoyu paylaşabilirim. Hangi ürünleri ve rakip sitelerini takip etmek istersiniz?

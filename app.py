import io
import json
import os
import re
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from time import mktime
from urllib.parse import quote

import feedparser
import requests
import trafilatura
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request, send_file

import report as report_mod

app = Flask(__name__)

CITY = "Kars"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=tr&gl=TR&ceid=TR:tr"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; HaberTakipSistemi/1.0)"}

# --- Proxy (paylaşımlı bulut IP'lerinde Google/haber sitesi engelini aşmak için) ---
# PROXY_URL ör.: http://scraperapi:APIKEY@proxy-server.scraperapi.com:8001
# Boşsa doğrudan bağlanılır (localde gerek yok).
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
_PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None


def http_get(url, *, proxied=False, **kw):
    """requests.get sarmalayıcısı. proxied=True ve PROXY_URL tanımlıysa
    istek temiz IP'li proxy üzerinden gider (proxy TLS'i MITM ettiği için
    verify kapatılır — yalnızca herkese açık sayfalar çekilir)."""
    if proxied and _PROXIES:
        kw.setdefault("proxies", _PROXIES)
        kw.setdefault("verify", False)
    return requests.get(url, **kw)


def http_post(url, *, proxied=False, **kw):
    if proxied and _PROXIES:
        kw.setdefault("proxies", _PROXIES)
        kw.setdefault("verify", False)
    return requests.post(url, **kw)


warnings.filterwarnings("ignore", message="Unverified HTTPS request")
try:
    requests.packages.urllib3.disable_warnings()  # proxy MITM'de verify=False gürültüsü
except Exception:
    pass

QUICK_KEYWORDS = ["nükleer", "sınır", "tatbikat", "deprem", "hudut", "PKK"]

# Kritik Bilgi İhtiyacı (KBİ) modülü — sabit sorgulu izleme kategorileri
KBI_CATEGORIES = [
    {
        "key": "kars_gundem",
        "title": "Kars Gündem",
        "desc": "Son 24 saat içinde ile dair çıkan haberler",
        "query": '"Kars"',
        "hours": 24,
        "limit": 10,
    },
    {
        "key": "sinir_hatti",
        "title": "Sınır Kapısı / Hudut Hattı",
        "desc": "Kars ve çevresindeki sınır kapısı, hudut, geçiş haberleri",
        "query": '"Kars" ("sınır kapısı" OR hudut OR sınır OR gümrük)',
        "hours": None,
        "limit": 8,
    },
    {
        "key": "ermenistan",
        "title": "Ermenistan",
        "desc": "Ermenistan kaynaklı bölgesel/sınır gelişmeleri",
        "query": 'Ermenistan (Kars OR Türkiye OR sınır OR hudut)',
        "hours": None,
        "limit": 8,
    },
    {
        "key": "azerbaycan",
        "title": "Azerbaycan",
        "desc": "Azerbaycan kaynaklı bölgesel/sınır gelişmeleri",
        "query": 'Azerbaycan (Kars OR Türkiye OR sınır OR hudut)',
        "hours": None,
        "limit": 8,
    },
    {
        "key": "gurcistan",
        "title": "Gürcistan",
        "desc": "Gürcistan kaynaklı bölgesel/sınır gelişmeleri",
        "query": 'Gürcistan (Kars OR Türkiye OR sınır OR hudut)',
        "hours": None,
        "limit": 8,
    },
    {
        "key": "pkk_teror",
        "title": "PKK / Terörle Mücadele",
        "desc": "Bölge ve hudut hattında terör örgütü faaliyeti, operasyon ve güvenlik gelişmeleri",
        "query": '(PKK OR terör OR "terörle mücadele" OR operasyon) (Kars OR hudut OR sınır OR "Doğu Anadolu" OR Ardahan OR Iğdır)',
        "hours": None,
        "limit": 8,
    },
    {
        "key": "deprem_afad",
        "title": "Deprem / AFAD / Afet",
        "desc": "Bölgede deprem, doğal afet, AFAD duyuru ve müdahaleleri",
        "query": '(deprem OR AFAD OR sel OR heyelan OR "doğal afet" OR çığ) (Kars OR Ardahan OR Iğdır OR "Doğu Anadolu")',
        "hours": None,
        "limit": 8,
    },
    {
        "key": "tatbikat",
        "title": "Askeri Tatbikat / Hareketlilik",
        "desc": "Bölgesel askeri tatbikat, konuşlanma ve hareketlilik haberleri",
        "query": '(tatbikat OR "askeri tatbikat" OR "kuvvet kaydırma" OR konuşlanma OR "sınır ötesi") (Kars OR Türkiye OR Ermenistan OR Azerbaycan OR Gürcistan OR hudut)',
        "hours": None,
        "limit": 8,
    },
]


def _fetch_rss(query: str):
    url = GOOGLE_NEWS_RSS.format(query=quote(query))
    response = requests.get(url, headers=HEADERS, timeout=10)
    response.raise_for_status()
    return feedparser.parse(response.content)


def search_raw(query: str, hours: int | None = None, limit: int | None = None):
    feed = _fetch_rss(query)
    now = datetime.now()

    results = []
    for entry in feed.entries:
        title = entry.get("title", "")
        source = ""
        if " - " in title:
            title, source = title.rsplit(" - ", 1)

        parsed = entry.get("published_parsed")
        dt = datetime.fromtimestamp(mktime(parsed)) if parsed else None

        if hours is not None and (dt is None or now - dt > timedelta(hours=hours)):
            continue

        results.append({
            "title": title,
            "source": source or "Bilinmeyen Kaynak",
            "link": entry.get("link", ""),
            "published": dt.strftime("%d.%m.%Y %H:%M") if dt else entry.get("published", "—"),
            "sort_key": dt or datetime.min,
        })

    results.sort(key=lambda r: r["sort_key"], reverse=True)
    return results[:limit] if limit else results


def search_news(keyword: str):
    query = f'"{CITY}" "{keyword}"' if keyword else f'"{CITY}"'
    return search_raw(query)


TURKISH_STOPWORDS = {
    "ve", "bir", "bu", "şu", "o", "da", "de", "ki", "ile", "için", "gibi",
    "ama", "fakat", "çünkü", "veya", "ya", "mi", "mı", "mu", "mü", "ona",
    "onun", "olan", "olarak", "olduğu", "olduğunu", "olmadığını", "en",
    "çok", "daha", "sonra", "önce", "kadar", "göre", "arasında", "ise",
    "ancak", "hem", "şey", "ne", "nasıl", "niçin", "neden", "her", "tüm",
    "bütün", "diye", "dedi", "dediği", "belirtti", "açıkladı", "ifade",
    "etti", "yaptı", "yapıldı", "oldu", "olacak", "var", "yok", "biri",
    "kendi", "tarafından", "üzere", "yine", "ayrıca", "böylece", "sırasında",
    "bazı", "hangi", "değil", "ise", "iken", "dahil", "üzerine", "yer",
    "aldı", "the", "and", "for", "with", "from",
}


# Datacenter IP'lerinde (Render vb.) Google, önce çerez onay sayfasına
# yönlendiriyor; bu çerezler onay ekranını atlatır.
_GOOGLE_COOKIES = {"CONSENT": "YES+cb", "SOCS": "CAI"}
_GOOGLE_HEADERS = {**HEADERS, "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.6"}

# Kaynak çözümlenemediğinde Google News'in kendi genel açıklaması metin
# sanılıp özete giriyordu — bu kalıpları "içerik yok" say.
_GOOGLE_BOILERPLATE = (
    "aggregated from sources all over the world by google news",
    "comprehensive up-to-date news coverage",
    "google haberler",
)


def _resolve_google_news(article_url: str) -> tuple[str, dict]:
    """Google News RSS yönlendirme token'ını gerçek yayıncı URL'sine çevirir.
    (url, teşhis) döner; url çözümlenemezse ''."""
    dbg = {}
    if "news.google.com" not in article_url:
        return article_url, {"skip": "already-direct"}

    art_id = article_url.rstrip("/").split("/")[-1].split("?")[0]

    # Yöntem A — sayfadaki gömülü JSON'dan (data-n-a-*) + batchexecute
    # İlk GET Render'dan doğrudan çalışıyor; sadece batchexecute POST engelli,
    # o yüzden proxy kredisini yalnızca POST'ta harcarız.
    try:
        page = http_get(
            f"https://news.google.com/rss/articles/{art_id}",
            headers=_GOOGLE_HEADERS, cookies=_GOOGLE_COOKIES, timeout=15,
        )
        dbg["A_get_status"] = page.status_code
        dbg["A_len"] = len(page.text)
        dbg["A_has_cwiz"] = "c-wiz" in page.text
        dbg["A_consent"] = "consent.google.com" in page.url or "CONSENT" in page.text[:2000]
        soup = BeautifulSoup(page.text, "html.parser")
        div = soup.select_one("c-wiz > div[jscontroller]")
        if div and div.get("data-n-a-id"):
            sig, ts, b64 = div.get("data-n-a-sg"), div.get("data-n-a-ts"), div.get("data-n-a-id")
            payload = [
                "Fbv4je",
                '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,'
                'null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
                f'"{b64}",{ts},"{sig}"]',
            ]
            resp = http_post(
                "https://news.google.com/_/DotsSplashUi/data/batchexecute",
                proxied=True,
                headers={**_GOOGLE_HEADERS, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                cookies=_GOOGLE_COOKIES, data={"f.req": json.dumps([[payload]])}, timeout=20,
            )
            dbg["A_post_status"] = resp.status_code
            parsed = json.loads(resp.text.split("\n\n")[1])
            real = json.loads(parsed[0][2])[1]
            dbg["A_real"] = (real or "")[:120]
            if real and "news.google.com" not in real:
                return real, dbg
        else:
            dbg["A_div"] = "not-found"
    except Exception as e:
        dbg["A_err"] = f"{type(e).__name__}: {e}"[:200]

    # Yöntem B — RSS öğesinin kendi yönlendirmesini takip et
    try:
        r = http_get(
            f"https://news.google.com/articles/{art_id}",
            proxied=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"},
            timeout=20, allow_redirects=True,
        )
        dbg["B_status"] = r.status_code
        dbg["B_final"] = r.url[:120]
        if "news.google.com" not in r.url:
            return r.url, dbg
        m = re.search(r'data-n-au="([^"]+)"', r.text) or re.search(r'<c-wiz[^>]+data-p="[^"]*(https?[^"&]+)', r.text)
        if m:
            cand = m.group(1).replace("&amp;", "&")
            dbg["B_meta"] = cand[:120]
            if cand.startswith("http") and "news.google.com" not in cand:
                return cand, dbg
    except Exception as e:
        dbg["B_err"] = f"{type(e).__name__}: {e}"[:200]

    return "", dbg


def resolve_google_news_url(article_url: str) -> str:
    return _resolve_google_news(article_url)[0]


def _truncate_at_next_section(text: str) -> str:
    """Turkish köşe yazısı pages often stack several unrelated items under one
    URL, separated by an ALL-CAPS sub-heading. Keep only the first section so
    the summary doesn't drift into an unrelated story."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if i == 0:
            continue
        stripped = line.strip()
        letters = [c for c in stripped if c.isalpha()]
        if len(letters) >= 4 and stripped == stripped.upper() and len(stripped) < 90:
            return "\n".join(lines[:i])
    return text


# Haber sitelerinde asıl metnin ardına eklenen "ilgili haberler / son dakika"
# widget'larının başladığı yeri yakalayan işaretler.
_WIDGET_MARKERS = [
    re.compile(r"\n\s*(?:Bugün|Dün|Bu gün)[,;]?\s*\d{1,2}[:.]\d{2}", re.IGNORECASE),
    re.compile(r"\n\s*\d{1,2}\s*(?:saat|dakika|gün)\s+önce", re.IGNORECASE),
    re.compile(r"\n\s*[-–—]\s*\n\s*[-–—]\s*\n"),  # arka arkaya ayraç satırları
    re.compile(r"\n\s*(?:İlgili Haberler|İLGİLİ HABERLER|Son Dakika Haberleri|Öne Çıkan Haberler|Daha Fazla)\s*\n"),
    # sosyal paylaşım / künye kırıntıları (satır içi de olabilir)
    re.compile(r"\s*•?\s*\d{1,2}\s+\w+\s+20\d{2}\s+Haberi?\s+Paylaş"),
    re.compile(r"\s*(?:Haberi Paylaş|Bu haberi paylaş|Yorumlar\b|Yorum Yap\b)"),
    re.compile(r"\s*•\s*\d{1,2}\s+\w+\s+20\d{2}\b"),
]

# Yazar/tarih künyesi satırları ("29 Ağustos 2026•Güncelleme: ...", "Editör: ...")
_META_LINE_RE = re.compile(
    r"^\s*(?:\d{1,2}\s+\w+\s+20\d{2}|Güncelleme\s*:|Editör\s*:|Yayınlanma\s*:|Giriş\s*:|Abone ol)\b.*$",
    re.IGNORECASE,
)

_DATELINE_RE = re.compile(r"^\s*\([A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜ\s./-]{1,30}\)\s*[-–—]\s*")
_BYLINE_RE = re.compile(r"^\s*(?:Haber\s*:|Haberi?\s+yapan\s*:|Foto(?:ğraf)?\s*:|Kaynak\s*:)\s*.*$", re.IGNORECASE)


def _strip_article_noise(text: str) -> str:
    """Byline / dateline ve makale sonuna eklenen ilgili-haber widget'larını atar."""
    if not text:
        return text

    cut = len(text)
    for rx in _WIDGET_MARKERS:
        m = rx.search(text)
        if m and m.start() < cut:
            cut = m.start()
    text = text[:cut].strip()

    lines = [ln for ln in text.split("\n") if not _META_LINE_RE.match(ln.strip())]
    # baştaki byline / dateline satırlarını temizle
    while lines:
        first = lines[0].strip()
        if not first or _BYLINE_RE.match(first):
            lines.pop(0)
            continue
        stripped = _DATELINE_RE.sub("", lines[0])
        if stripped != lines[0]:
            lines[0] = stripped
        break
    # sondaki clickbait promo kırıntılarını / byline'ları at
    while lines and (
        lines[-1].strip().endswith("...")
        or lines[-1].strip() in {"-", "–", "—", ""}
        or _BYLINE_RE.match(lines[-1].strip())
        or (len(lines[-1].strip()) <= 5 and lines[-1].strip().isupper())  # ajans rumuzu: "AA", "İHA"
    ):
        lines.pop()

    return "\n".join(lines).strip()


def _fetch_article_text(real_url: str) -> str:
    """Yayıncı sayfasını önce doğrudan, boş/başarısız gelirse proxy ile çeker."""
    for proxied in ((False, True) if _PROXIES else (False,)):
        try:
            resp = http_get(
                real_url, proxied=proxied, headers=HEADERS, timeout=20, allow_redirects=True
            )
            resp.raise_for_status()
            txt = extract_article_text(resp.text)
            if txt:
                return txt
        except requests.RequestException:
            continue
    return ""


def extract_article_text(html: str) -> str:
    text = trafilatura.extract(html, favor_precision=True, include_comments=False) or ""
    text = _truncate_at_next_section(text)
    text = _strip_article_noise(text)

    if len(text) < 150:
        soup = BeautifulSoup(html, "html.parser")
        meta = soup.find("meta", attrs={"name": "description"}) or soup.find(
            "meta", attrs={"property": "og:description"}
        )
        if meta and meta.get("content"):
            fallback = meta["content"].strip()
            if len(fallback) > len(text):
                text = fallback

    if any(b in text.lower() for b in _GOOGLE_BOILERPLATE):
        return ""  # Google News kabuğu geldi — kaynak açılamadı say

    return text[:20000]


def summarize_text(text: str, title: str = "", max_sentences: int = 3) -> str:
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if len(s.strip()) > 20]
    if not sentences:
        return ""
    if len(sentences) <= max_sentences:
        return " ".join(sentences)

    words = re.findall(r"[a-zA-ZçÇğĞıİöÖşŞüÜ]+", text.lower())
    freq = {}
    for w in words:
        if w in TURKISH_STOPWORDS or len(w) < 3:
            continue
        freq[w] = freq.get(w, 0) + 1

    if not freq:
        return " ".join(sentences[:max_sentences])

    top_freq = max(freq.values())
    for w in freq:
        freq[w] /= top_freq

    title_words = {
        w for w in re.findall(r"[a-zA-ZçÇğĞıİöÖşŞüÜ]+", title.lower())
        if w not in TURKISH_STOPWORDS and len(w) >= 3
    }

    scored = []
    for i, sent in enumerate(sentences):
        sent_words = re.findall(r"[a-zA-ZçÇğĞıİöÖşŞüÜ]+", sent.lower())
        base_score = sum(freq.get(w, 0) for w in sent_words) / (len(sent_words) ** 0.5 or 1)
        title_overlap = sum(1 for w in sent_words if w in title_words)
        scored.append((base_score + title_overlap * 0.8, i, sent))

    ranked = sorted(scored, key=lambda x: x[0], reverse=True)
    chosen, seen = [], []
    for _, idx, sent in ranked:
        toks = set(re.findall(r"[a-zA-ZçÇğĞıİöÖşŞüÜ]+", sent.lower()))
        if any(toks and len(toks & p) / len(toks) > 0.8 for p in seen):
            continue  # neredeyse aynı cümleyi tekrar ekleme
        seen.append(toks)
        chosen.append((idx, sent))
        if len(chosen) >= max_sentences:
            break
    chosen.sort(key=lambda x: x[0])
    return " ".join(s for _, s in chosen)


# --- 5N1K (Kim / Ne / Nerede / Ne zaman / Neden / Nasıl) otomatik çıkarım ---

PLACE_HINTS = [
    "Kars", "Ardahan", "Iğdır", "Sarıkamış", "Selim", "Kağızman", "Digor", "Susuz",
    "Akyaka", "Arpaçay", "Çıldır", "Posof", "Göle", "Hanak", "Damal", "Aralık",
    "Dilucu", "Türkgözü", "Aktaş Sınır Kapısı", "Aktaş", "Alican", "Gürbulak",
    "Nahçıvan", "hudut hattı", "hudut", "sınır kapısı", "sınır hattı",
    "Doğu Anadolu", "Kafkas", "Ermenistan sınırı", "Gürcistan sınırı",
]

ACTOR_HINTS = [
    "AFAD", "AKUT", "Kızılay", "Valilik", "Vali", "Kaymakamlık", "Kaymakam",
    "Belediye", "Büyükşehir", "İl Özel İdaresi", "TSK", "MSB", "Milli Savunma Bakanlığı",
    "Kara Kuvvetleri", "Jandarma", "Jandarma Genel Komutanlığı", "Emniyet",
    "polis", "Sahil Güvenlik", "hudut birlikleri", "hudut karakolu",
    "Bakanlık", "Bakan", "Cumhurbaşkanı", "Genelkurmay", "PKK", "terör örgütü",
    "Meteoroloji", "Meteoroloji Genel Müdürlüğü", "DSİ", "Karayolları",
    "Kafkas Üniversitesi", "İçişleri Bakanlığı", "Sağlık Bakanlığı", "MİT",
    "gümrük", "Ticaret Bakanlığı", "TCDD", "DHMİ",
]

CAUSE_MARKERS = [
    "nedeniyle", "dolayısıyla", "sebebiyle", "yüzünden", "dolayı", "kaynaklı",
    "amacıyla", "gerekçesiyle", "sonucu ortaya", "bağlı olarak",
]

MANNER_MARKERS = [
    "tarafından", "sonucunda", "neticesinde", "operasyonla", "operasyon düzenlen",
    "çalışma başlat", "ekipler", "sevk edil", "müdahale", "tahliye edil",
    "kurtarıl", "gözaltına al", "düzenlenen", "yürütülen",
]


def _find_hints(haystack: str, hints):
    low = haystack.lower()
    found = []
    for h in hints:
        pos = low.find(h.lower())
        if pos != -1:
            found.append((pos, h))
    found.sort(key=lambda x: x[0])
    seen, out = set(), []
    for _, h in found:
        key = h.lower()
        if key not in seen:
            seen.add(key)
            out.append(h)
    # başka bir eşleşmenin alt dizesi olanları ele (Kaymakam ⊂ Kaymakamlık)
    return [h for h in out if not any(h.lower() in o.lower() and h.lower() != o.lower() for o in out)]


def _first_sentence_with(sentences, markers, skip=""):
    skip_n = re.sub(r"\s+", " ", skip).strip().lower().rstrip(".")
    for s in sentences:
        low = s.lower()
        if re.sub(r"\s+", " ", low).strip().rstrip(".") == skip_n:
            continue
        if any(m in low for m in markers):
            return re.sub(r"\s+", " ", s).strip()[:240]
    return ""


def compose_5n1k(text: str, title: str = "", published: str = "") -> dict:
    """Haber metninden tek paragraflık bir 5N1K özet taslağı üretir ve
    ayrıca analiste yardımcı olacak 'ipucu' alanlarını çıkarır. Rapora
    yalnızca 'ozet' paragrafı girer; ipuçları ekranda gösterilir."""
    body = (text or "").strip()
    # trafilatura bazen H1 başlığı gövdenin ilk satırı olarak veriyor — tekrarı at
    norm = lambda s: re.sub(r"\s+", " ", s).strip().lower().rstrip(".")
    body_lines = body.split("\n")
    if body_lines and title and norm(body_lines[0]) == norm(title):
        body = "\n".join(body_lines[1:]).strip()

    full = f"{title}. {body}".strip()
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", full) if len(s.strip()) > 15]

    ozet = summarize_text(body, title=title, max_sentences=3) if body else ""
    ozet = re.sub(r"\s+", " ", ozet).strip()
    if not ozet:
        ozet = title.strip()

    return {
        "ozet": ozet,
        "ipucu": {
            "kim": ", ".join(_find_hints(full, ACTOR_HINTS)),
            "nerede": ", ".join(_find_hints(full, PLACE_HINTS)),
            "ne_zaman": published,
            "neden": _first_sentence_with(sentences, CAUSE_MARKERS, skip=title),
            "nasil": _first_sentence_with(sentences, MANNER_MARKERS, skip=title),
        },
    }


@app.route("/")
def index():
    keyword = request.args.get("keyword", "").strip()
    searched = bool(keyword or request.args.get("searched"))
    results = search_news(keyword) if searched else []
    return render_template(
        "index.html",
        city=CITY,
        keyword=keyword,
        results=results,
        searched=searched,
        quick_keywords=QUICK_KEYWORDS,
        generated_at=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        active="search",
    )


@app.route("/kbi")
def kbi():
    def run(category):
        try:
            news = search_raw(category["query"], hours=category["hours"], limit=category["limit"])
            error = None
        except Exception:
            news = []
            error = "Kaynağa ulaşılamadı"
        return {**category, "news": news, "error": error}

    with ThreadPoolExecutor(max_workers=len(KBI_CATEGORIES)) as pool:
        modules = list(pool.map(run, KBI_CATEGORIES))

    return render_template(
        "kbi.html",
        city=CITY,
        modules=modules,
        generated_at=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        active="kbi",
    )


@app.route("/rapor")
def rapor():
    """Rapor hazırlama ekranı: kapsanan dönemdeki haberleri kategori kategori
    listeler; analist haberleri seçip 5N1K özetini girer."""
    start, end = report_mod.report_period()

    def run(category):
        try:
            raw = search_raw(category["query"], hours=None, limit=None)
            news = report_mod.filter_window(raw, start, end)
            error = None
        except Exception:
            news = []
            error = "Kaynağa ulaşılamadı"
        return {**category, "news": news, "error": error}

    with ThreadPoolExecutor(max_workers=len(KBI_CATEGORIES)) as pool:
        modules = list(pool.map(run, KBI_CATEGORIES))

    # tüm haberlere rapor genelinde tekil indeks ver
    rows = []
    for m in modules:
        for n in m["news"]:
            rows.append({
                "idx": len(rows),
                "category": m["title"],
                "title": n["title"],
                "source": n["source"],
                "link": n["link"],
                "published": n["published"],
            })

    return render_template(
        "rapor.html",
        city=CITY,
        modules=modules,
        rows=rows,
        period_start=report_mod._fmt(start),
        period_end=report_mod._fmt(end),
        generated_at=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        active="rapor",
    )


@app.route("/rapor/olustur", methods=["POST"])
def rapor_olustur():
    """Seçilen haberleri ve 5N1K özetlerini alıp .docx üretir."""
    f = request.form
    selected = []
    for idx in f.getlist("sel"):
        p = f.get(f"published_{idx}", "").strip()
        try:
            sort_key = datetime.strptime(p, "%d.%m.%Y %H:%M")
        except ValueError:
            sort_key = datetime.min
        selected.append({
            "category": f.get(f"category_{idx}", "").strip(),
            "title": f.get(f"title_{idx}", "").strip(),
            "source": f.get(f"source_{idx}", "").strip(),
            "published": p,
            "sort_key": sort_key,
            "ozet": f.get(f"ozet_{idx}", "").strip(),
        })

    if not selected:
        return "Rapora en az bir haber seçilmelidir.", 400

    if not any(it["ozet"] for it in selected):
        return "Seçilen haberler için 5N1K özeti girilmemiş.", 400

    data = report_mod.build_report(selected, quick_keywords=QUICK_KEYWORDS)
    return send_file(
        io.BytesIO(data),
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        as_attachment=True,
        download_name=report_mod.report_filename(),
    )


@app.route("/debug/resolve")
def debug_resolve():
    """Geçici teşhis: Render'ın Google News çözümlemesinde nerede takıldığını gösterir."""
    url = request.args.get("url", "").strip()
    if not url:
        feed = _fetch_rss('"Kars"')
        url = feed.entries[0].get("link", "") if feed.entries else ""
    real, dbg = _resolve_google_news(url)
    out = {"input": url[:120], "resolved": real[:200], "steps": dbg}
    out["proxy_configured"] = bool(_PROXIES)
    if real:
        txt = _fetch_article_text(real)
        out["text_len"] = len(txt)
        out["text_head"] = txt[:200]
    return jsonify(out)


@app.route("/analiz")
def analiz():
    """Bir haberin kaynağını tarayıp 5N1K alanlarını otomatik doldurur."""
    url = request.args.get("url", "").strip()
    title = request.args.get("title", "").strip()
    published = request.args.get("published", "").strip()
    if not url and not title:
        return jsonify({"error": "Haber bilgisi eksik."}), 400

    text = ""
    real_url = resolve_google_news_url(url) if url else ""
    if real_url:
        text = _fetch_article_text(real_url)

    result = compose_5n1k(text, title=title, published=published)
    result["kaynak_tarandi"] = bool(text)
    if not text:
        result["not"] = "Kaynak metnine ulaşılamadı; özet haber başlığından üretildi, lütfen elle tamamlayın."
    return jsonify(result)


@app.route("/ozet")
def ozet():
    url = request.args.get("url", "").strip()
    title = request.args.get("title", "").strip()
    if not url:
        return jsonify({"error": "URL eksik."}), 400

    real_url = resolve_google_news_url(url)
    if not real_url:
        return jsonify({"error": "Haberin asıl kaynağı çözümlenemedi. Haberi doğrudan kaynağından görüntüleyin."}), 502

    text = _fetch_article_text(real_url)
    if not text:
        return jsonify({"error": "Bu kaynaktan özetlenebilir metin çıkarılamadı."}), 422

    summary = summarize_text(text, title=title)
    if not summary:
        return jsonify({"error": "Özet oluşturulamadı."}), 422

    return jsonify({"summary": summary, "final_url": real_url})


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5050))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)

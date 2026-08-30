import json
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from time import mktime
from urllib.parse import quote

import feedparser
import requests
import trafilatura
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

CITY = "Kars"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=tr&gl=TR&ceid=TR:tr"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; HaberTakipSistemi/1.0)"}

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
]


_EMPTY_FEED = feedparser.parse(b"<rss><channel></channel></rss>")
_RSS_CACHE: dict[str, tuple[float, object]] = {}
_RSS_TTL = 600  # sn


def _fetch_rss(query: str):
    """Google News RSS'i çeker. Google zaman zaman (özellikle bulut IP'lerine)
    429/503 döndürüyor; o durumda 500 yerine boş feed döner ve sayfa
    'sonuç yok' açılır. Sonuçlar 10 dk cache'lenir — ücretsiz dyno her
    uyandığında 8 kategoriyi aynı anda yeniden çekip zaman aşımına uğramasın."""
    now = time.time()
    hit = _RSS_CACHE.get(query)
    if hit and now - hit[0] < _RSS_TTL:
        return hit[1]

    url = GOOGLE_NEWS_RSS.format(query=quote(query))
    try:
        response = requests.get(url, headers=HEADERS, timeout=8)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        if feed.entries:
            _RSS_CACHE[query] = (now, feed)
        return feed
    except requests.RequestException:
        if hit:  # taze veri yoksa bayat da olsa eldekini ver
            return hit[1]
        return _EMPTY_FEED


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


def resolve_google_news_url(article_url: str) -> str:
    """Google News RSS links are opaque redirect tokens; decode them to the
    real publisher URL via Google's internal batchexecute endpoint."""
    if "news.google.com" not in article_url:
        return article_url

    try:
        page = requests.get(article_url, headers=HEADERS, timeout=8)
        page.raise_for_status()
        soup = BeautifulSoup(page.text, "html.parser")
        div = soup.select_one("c-wiz > div[jscontroller]")
        if not div:
            return article_url

        signature = div.get("data-n-a-sg")
        timestamp = div.get("data-n-a-ts")
        base64_str = div.get("data-n-a-id")
        if not (signature and timestamp and base64_str):
            return article_url

        payload = [
            "Fbv4je",
            '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,'
            'null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
            f'"{base64_str}",{timestamp},"{signature}"]',
        ]
        resp = requests.post(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            data={"f.req": json.dumps([[payload]])},
            timeout=8,
        )
        resp.raise_for_status()
        parsed = json.loads(resp.text.split("\n\n")[1])
        real_url = json.loads(parsed[0][2])[1]
        return real_url or article_url
    except Exception:
        return article_url


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


def extract_article_text(html: str) -> str:
    text = trafilatura.extract(html, favor_recall=True) or ""
    text = _truncate_at_next_section(text)

    if len(text) < 150:
        soup = BeautifulSoup(html, "html.parser")
        meta = soup.find("meta", attrs={"name": "description"}) or soup.find(
            "meta", attrs={"property": "og:description"}
        )
        if meta and meta.get("content"):
            fallback = meta["content"].strip()
            if len(fallback) > len(text):
                text = fallback

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

    top = sorted(scored, key=lambda x: x[0], reverse=True)[:max_sentences]
    top.sort(key=lambda x: x[1])
    return " ".join(s for _, _, s in top)


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


@app.route("/ozet")
def ozet():
    url = request.args.get("url", "").strip()
    title = request.args.get("title", "").strip()
    if not url:
        return jsonify({"error": "URL eksik."}), 400

    try:
        real_url = resolve_google_news_url(url)
        try:
            response = requests.get(real_url, headers=HEADERS, timeout=8)
            response.raise_for_status()
        except requests.RequestException:
            return jsonify({"error": "Kaynağa erişilemedi. Haberi doğrudan kaynağından görüntüleyin."}), 502

        text = extract_article_text(response.text)
        if not text:
            return jsonify({"error": "Bu kaynaktan özetlenebilir metin çıkarılamadı."}), 422

        summary = summarize_text(text, title=title)
        if not summary:
            return jsonify({"error": "Özet oluşturulamadı."}), 422

        return jsonify({"summary": summary, "final_url": response.url})
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Özet çıkarılırken beklenmeyen bir hata oluştu."}), 500


@app.errorhandler(Exception)
def _unhandled(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    traceback.print_exc()  # Render loglarına düşer
    wants_json = request.path.startswith("/ozet")
    if wants_json:
        return jsonify({"error": "Beklenmeyen bir hata oluştu."}), 500
    return (
        "<h1>Geçici bir hata oluştu</h1><p>Kaynak (Google Haberler) şu an yanıt "
        "vermiyor olabilir. Birkaç dakika sonra tekrar deneyin.</p>",
        500,
    )


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5050))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)

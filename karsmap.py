"""Kars bölgesi şematik haritası — haber konumunu işaretleyip PNG üretir.

Word raporunda her haberin yanına konur. Pillow ile çizilir (harici tile
servisi / sistem kütüphanesi yok). Bölge kimlikleri app.detect_region ile
aynıdır: 8 ilçe + ermenistan_siniri + komşu iller + kars_geneli / bolge_disi.
"""

import io

from PIL import Image, ImageDraw, ImageFont

# 260 x 210'luk şema; PNG bu ölçeğin S katı çizilir.
S = 3
W, H = 260 * S, 210 * S

REGION_LABELS = {
    "merkez": "Kars (Merkez)", "sarikamis": "Sarıkamış", "kagizman": "Kağızman",
    "digor": "Digor", "selim": "Selim", "susuz": "Susuz", "akyaka": "Akyaka",
    "arpacay": "Arpaçay", "ermenistan_siniri": "Ermenistan sınır hattı",
    "margara": "Margara / Alican Geçiş Noktası", "dogukapi": "Doğukapı / Akhurik Hattı",
    "nahcivan": "Nahçıvan yönü", "gurcistan": "Gürcistan yönü", "ermenistan": "Ermenistan",
    "ardahan": "Ardahan", "igdir": "Iğdır", "agri": "Ağrı", "erzurum": "Erzurum",
    "kars_geneli": "Kars geneli", "bolge_disi": "Bölge dışı",
}

# Analistin elle seçebileceği tüm konumlar (haritadaki sıraya yakın)
REGION_CHOICES = [
    "merkez", "sarikamis", "selim", "kagizman", "digor", "akyaka", "arpacay", "susuz",
    "ermenistan_siniri", "margara", "dogukapi", "ermenistan", "nahcivan", "gurcistan",
    "ardahan", "igdir", "agri", "erzurum", "kars_geneli", "bolge_disi",
]

BG = (23, 28, 34)
PROV_FILL = (30, 37, 45)
PROV_LINE = (42, 50, 60)
DOT = (90, 100, 112)
LABEL = (123, 136, 148)
BORDER = (91, 102, 115)
HI = (226, 89, 59)         # vurgu (kırmızı)
HI_DIM = (150, 60, 45)
WARN = (226, 185, 59)
WHITE = (238, 243, 247)

DISTRICTS = {
    "merkez": (130, 94, "Merkez"),
    "sarikamis": (46, 139, "Sarıkamış"),
    "selim": (92, 118, "Selim"),
    "kagizman": (135, 171, "Kağızman"),
    "digor": (181, 132, "Digor"),
    "akyaka": (215, 70, "Akyaka"),
    "arpacay": (166, 52, "Arpaçay"),
    "susuz": (106, 55, "Susuz"),
}

# (x, y, metin, döndürme_derece)
NEIGHBOURS = {
    "ardahan": (112, 13, "ARDAHAN", 0),
    "erzurum": (12, 108, "ERZURUM", 90),
    "agri": (98, 201, "AĞRI", 0),
    "igdir": (206, 198, "IĞDIR", 0),
    "ermenistan": (246, 116, "ERMENİSTAN", 90),
    "nahcivan": (224, 184, "NAHÇIVAN", 0),
    "gurcistan": (34, 14, "GÜRCİSTAN", 0),
}

PROV_POLY = [
    (118, 32), (150, 27), (185, 32), (215, 52), (230, 72), (235, 98),
    (228, 122), (210, 138), (202, 158), (180, 176), (150, 186), (126, 186),
    (98, 182), (66, 168), (44, 146), (32, 122), (30, 100), (40, 86),
    (58, 80), (70, 58), (92, 42),
]
# Ermenistan sınır hattı = poligonun KD–GD kenarı
BORDER_LINE = [
    (118, 32), (150, 27), (185, 32), (215, 52), (230, 72), (235, 98),
    (228, 122), (210, 138), (202, 158), (180, 176),
]

# Sınır geçiş noktaları (x, y, kısa etiket) — sınır hattı üzerinde
CROSSINGS = {
    "dogukapi": (231, 92, "Doğukapı"),
    "margara": (198, 178, "Margara"),
}


def _font(px):
    for path in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, px)
        except Exception:
            continue
    return ImageFont.load_default(size=px)


def _sc(p):
    return (p[0] * S, p[1] * S)


def _text(draw, xy, s, font, fill, anchor="mm", rotate=0):
    if not rotate:
        draw.text(_sc(xy), s, font=font, fill=fill, anchor=anchor)
        return
    bbox = font.getbbox(s)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 4
    tile = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text((pad - bbox[0], pad - bbox[1]), s, font=font, fill=fill)
    tile = tile.rotate(rotate, expand=True)
    cx, cy = _sc(xy)
    return tile, (int(cx - tile.width / 2), int(cy - tile.height / 2))


_CACHE = {}


def region_map_png(region: str) -> bytes:
    region = region or "kars_geneli"
    if region in _CACHE:
        return _CACHE[region]

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f_lbl = _font(15)
    f_nb = _font(14)
    f_cap = _font(19)

    disi = region == "bolge_disi"
    d.polygon([_sc(p) for p in PROV_POLY],
              fill=(26, 31, 37) if disi else PROV_FILL,
              outline=HI if region == "kars_geneli" else PROV_LINE,
              width=3 if region == "kars_geneli" else 2)

    # sınır hattı
    border_hi = region in ("ermenistan_siniri", "ermenistan", "margara", "dogukapi")
    d.line([_sc(p) for p in BORDER_LINE],
           fill=HI if border_hi else BORDER,
           width=6 if border_hi else 3,
           joint="curve")

    # sınır geçiş noktaları (eşkenar dörtgen işaret)
    for cid, (x, y, txt) in CROSSINGS.items():
        active = region == cid
        cx, cy = _sc((x, y))
        rr = (9 if active else 5) * S // 2
        d.polygon([(cx, cy - rr), (cx + rr, cy), (cx, cy + rr), (cx - rr, cy)],
                  fill=HI if active else WARN, outline=BG, width=2)
        if active:
            _text(d, (x, y + 12), txt, f_lbl, HI, anchor="mm")

    # komşular
    for rid, (x, y, txt, rot) in NEIGHBOURS.items():
        active = region == rid
        col = HI if active else LABEL
        r = _text(d, (x, y), txt, f_nb, col, rotate=rot)
        if rot and r:
            tile, pos = r
            img.paste(tile, pos, tile)

    # ilçeler
    for rid, (x, y, txt) in DISTRICTS.items():
        active = region == rid
        cx, cy = _sc((x, y))
        if active:
            rr = 7 * S
            d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=HI, width=2)
        rr = (5 if active else 3) * S // 2 + (2 if active else 0)
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=HI if active else DOT)
        _text(d, (x, y - 8), txt, f_lbl, WHITE if active else LABEL, anchor="ms")

    if region == "bolge_disi":
        _text(d, (130, 150), "KONUM: BÖLGE DIŞI", f_cap, WARN, anchor="mm")

    out = io.BytesIO()
    img.save(out, format="PNG")
    _CACHE[region] = out.getvalue()
    return _CACHE[region]


if __name__ == "__main__":
    for r in ("kagizman", "ermenistan_siniri", "margara", "dogukapi", "bolge_disi",
              "ardahan", "kars_geneli"):
        open(f"/tmp/km_{r}.png", "wb").write(region_map_png(r))
        print("wrote", r)

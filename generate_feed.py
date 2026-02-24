# generate_feed.py
import re
import html
import unicodedata
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from lxml import etree

# =========================
# CONFIG
# =========================
BASE_FEED_URL = "https://www.pccold.com.br/xml/6301c/googlemerchant.xml"
OUTPUT_FILE = "google-merchant.xml"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
TIMEOUT = 25

DESC_TARGET_MIN = 280
DESC_TARGET_MAX = 850

MAX_ADDITIONAL_IMAGES = 10

# palavras que indicam “lixo” de imagem/estrutura
BAD_IMAGE_SUBSTRINGS = [
    "produto-sem-imagem",
    "struct/",
    "camp_google_safe_browsing",
    "static/img/",
    "favicon",
    "logo",
    "sprite",
]


# =========================
# HELPERS (TEXTO)
# =========================
def remove_emojis_and_controls(text: str) -> str:
    if not text:
        return ""
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Cc":
            continue
        if cat in ("So",):  # símbolos (inclui muitos emojis)
            continue
        out.append(ch)
    text = "".join(out)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def kill_css_like_noise(text: str) -> str:
    """
    Remove qualquer coisa que pareça CSS:
    - declarações "prop: value;"
    - trechos com "px", "font-family", "border-collapse", etc.
    - seletores ".classe h1", "ul li::before", etc.
    """
    if not text:
        return ""

    # remove blocos { ... }
    text = re.sub(r"\{[^{}]*\}", " ", text)

    # remove declarações CSS "prop: value;" repetidas
    text = re.sub(r"\b[a-zA-Z-]+\s*:\s*[^;]{1,80};", " ", text)

    # remove seletores comuns e pseudo-elementos
    text = re.sub(r"\b(h1|h2|h3|ul|ol|li|table|thead|tbody|tr|td|th|span|div)\b\s*(::before|::after)?", " ", text, flags=re.I)
    text = re.sub(r"\.[a-zA-Z0-9_-]+", " ", text)  # .classe

    # mata palavras típicas de CSS que sobram
    css_words = [
        "font-family", "border-collapse", "padding-left", "list-style",
        "margin", "color", "width", "height", "display", "align-items",
        "justify-content", "line-height", "sans-serif", "px", "rem"
    ]
    for w in css_words:
        text = re.sub(rf"\b{re.escape(w)}\b", " ", text, flags=re.I)

    # limpa sobras de pontuação estranha
    text = re.sub(r"\s+", " ", text).strip()
    return text


def html_to_clean_text(raw: str) -> str:
    """Converte HTML em texto e remove CSS/ruído."""
    if not raw:
        return ""
    raw = html.unescape(raw)

    # remove tags style e scripts antes do parse
    raw = re.sub(r"<style.*?>.*?</style>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<script.*?>.*?</script>", " ", raw, flags=re.I | re.S)

    soup = BeautifulSoup(raw, "html.parser")
    text = soup.get_text(" ", strip=True)
    text = remove_emojis_and_controls(text)
    text = kill_css_like_noise(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_specs(text: str) -> list[str]:
    """
    Extrai specs comuns (não inventa, só pega se existir no texto).
    """
    if not text:
        return []

    specs = []

    patterns = [
        (r"\b(\d+(?:[.,]\d+)?)\s*(w/mk)\b", "Condutividade {0} {1}"),
        (r"\b(\d{3,5})\s*(rpm)\b", "Rotação {0} {1}"),
        (r"\b(\d+(?:[.,]\d+)?)\s*(cfm)\b", "Fluxo de ar {0} {1}"),
        (r"\b(\d+(?:[.,]\d+)?)\s*(dba)\b", "Ruído {0} {1}"),
        (r"\b(\d+(?:[.,]\d+)?)\s*(mm)\b", None),  # muito comum, vamos filtrar depois
        (r"\b(\d+(?:[.,]\d+)?)\s*(g|gramas)\b", "Conteúdo {0} {1}"),
        (r"\b(\d+(?:[.,]\d+)?)\s*(ml)\b", "Conteúdo {0} {1}"),
        (r"\b(\d+)\s*(anos)\b", "Garantia {0} {1}"),
    ]

    for pat, fmt in patterns:
        for m in re.finditer(pat, text, flags=re.I):
            v1 = m.group(1).replace(",", ".")
            v2 = m.group(2).lower()
            if fmt:
                specs.append(fmt.format(v1, v2.upper() if v2 in ["w/mk", "cfm", "dba", "rpm"] else v2))
            else:
                # mm: só guarda se parecer dimensão útil (ex: 120mm/140mm/240mm/360mm)
                try:
                    mm = float(v1)
                    if mm in [80, 92, 120, 140, 200, 240, 280, 360, 420]:
                        specs.append(f"Dimensão {int(mm)}mm")
                except Exception:
                    pass

    # remove duplicados mantendo ordem
    out = []
    seen = set()
    for s in specs:
        key = s.lower()
        if key not in seen:
            out.append(s)
            seen.add(key)

    return out[:4]  # não exagera


def fetch_product_page_text(product_url: str) -> str:
    """Fallback: pega texto da página do produto (quando a descrição do XML for ruim)."""
    if not product_url:
        return ""
    try:
        r = requests.get(product_url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")

        # tenta pegar bloco de descrição (heurístico)
        # se não achar, pega texto do body (mas bem limitado)
        candidates = []
        for sel in [
            ".descricao-produto",
            "#descricao",
            "[itemprop='description']",
            ".product-description",
        ]:
            node = soup.select_one(sel)
            if node:
                candidates.append(node.get_text(" ", strip=True))

        if candidates:
            return html_to_clean_text(" ".join(candidates))

        # fallback mais genérico
        body = soup.get_text(" ", strip=True)
        return kill_css_like_noise(remove_emojis_and_controls(body))
    except Exception:
        return ""


def build_unique_description(title: str, brand: str, product_type: str, mpn: str, gtin: str,
                            xml_desc_raw: str, product_url: str) -> str:
    """
    Descrição única e “profissional”:
    - base: dados do produto + resumo real da descrição
    - specs extraídas (se existirem)
    """
    title = (title or "").strip()
    brand = (brand or "").strip()
    product_type = (product_type or "").strip()
    mpn = (mpn or "").strip()
    gtin = (gtin or "").strip()

    # 1) Texto base (do XML)
    desc_clean = html_to_clean_text(xml_desc_raw)

    # Se a descrição do XML estiver vazia ou “curta demais”, tenta pegar da página do produto
    if len(desc_clean) < 120:
        page_text = fetch_product_page_text(product_url)
        if len(page_text) > len(desc_clean):
            desc_clean = page_text

    # 2) Header (já garante unicidade)
    header_parts = []
    if title:
        header_parts.append(title)
    if brand and brand.lower() not in title.lower():
        header_parts.append(f"Marca {brand}")
    if mpn:
        header_parts.append(f"MPN {mpn}")
    if gtin:
        header_parts.append(f"EAN {gtin}")
    header = " - ".join(header_parts) + "."

    # 3) Resumo do conteúdo real (sem repetir o header)
    # pega um trecho bom do começo
    body = desc_clean
    if body:
        # remove o próprio título do começo se estiver repetindo
        body = re.sub(re.escape(title), " ", body, flags=re.I).strip()
        body = re.sub(r"\s+", " ", body).strip()

    snippet = ""
    if body:
        snippet = body[:600]
        # tenta cortar no último ponto
        cut = snippet.rfind(".")
        if cut > 200:
            snippet = snippet[:cut + 1]
        snippet = snippet.strip()

    # 4) Specs (se houver)
    specs = extract_specs(desc_clean)
    specs_line = ""
    if specs:
        specs_line = " | ".join(specs).strip() + "."

    # 5) Categoria (opcional e curta)
    category_line = ""
    if product_type:
        category_line = f"Categoria: {product_type}."

    final = " ".join([header, snippet, specs_line, category_line]).strip()
    final = re.sub(r"\s+", " ", final).strip()

    # garante tamanho mínimo sem frase padrão repetida (usa o que tem do produto)
    if len(final) < DESC_TARGET_MIN and snippet:
        # repete mais um pedaço do body (ainda do próprio produto)
        extra = body[600:900].strip()
        if extra:
            final = (final + " " + extra[:250]).strip()

    # limita max
    if len(final) > DESC_TARGET_MAX:
        final = final[:DESC_TARGET_MAX].rstrip()
        last_dot = final.rfind(".")
        if last_dot > 250:
            final = final[:last_dot + 1]

    return final


# =========================
# HELPERS (IMAGENS)
# =========================
def force_high_res_cdn(url: str) -> str:
    if not url:
        return url
    return re.sub(r"/\d{2,4}x\d{2,4}/", "/1600x1600/", url)


def is_bad_image(url: str) -> bool:
    u = (url or "").lower()
    return any(bad in u for bad in BAD_IMAGE_SUBSTRINGS)


def extract_gallery_images_from_product_page(product_url: str) -> list[str]:
    if not product_url:
        return []
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get(product_url, headers=headers, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        urls = []

        # og:image
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            urls.append(og["content"].strip())

        # img tags
        for img in soup.select("img"):
            cand = (img.get("src") or "").strip() or (img.get("data-src") or "").strip()
            if not cand:
                continue
            if "cdn.awsli.com.br" not in cand:
                continue
            if is_bad_image(cand):
                continue
            urls.append(cand)

        # normaliza, high-res e remove duplicadas mantendo ordem
        fixed = []
        seen = set()
        for u in urls:
            u2 = force_high_res_cdn(u)
            if u2.lower() in seen:
                continue
            seen.add(u2.lower())
            fixed.append(u2)

        return fixed
    except Exception:
        return []


# =========================
# XML HELPERS
# =========================
def get_text(el, tag):
    found = el.find(tag)
    if found is None or found.text is None:
        return ""
    return found.text.strip()


def set_cdata_text(el, text: str):
    el.text = etree.CDATA(text)


# =========================
# MAIN
# =========================
def main():
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(BASE_FEED_URL, headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()

    parser = etree.XMLParser(recover=True, remove_blank_text=True)
    root = etree.fromstring(resp.content, parser=parser)

    nsmap = root.nsmap.copy()
    gns = nsmap.get("g") or "http://base.google.com/ns/1.0"
    nsmap["g"] = gns
    g = "{%s}" % gns

    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("XML inválido: não encontrei <channel>.")

    items = channel.findall("item")
    print(f"Itens encontrados no XML base: {len(items)}")

    for item in items:
        title = get_text(item, "title")
        link = get_text(item, "link")

        brand = get_text(item, g + "brand")
        product_type = get_text(item, g + "product_type")

        pid = get_text(item, g + "id") or get_text(item, "g:id")

        # mpn = sku/id sempre
        mpn_el = item.find(g + "mpn")
        if mpn_el is None:
            mpn_el = etree.SubElement(item, g + "mpn")
        mpn_el.text = pid

        gtin = get_text(item, g + "gtin")

        # descrição otimizada e única
        desc_el = item.find("description")
        raw_desc = desc_el.text if desc_el is not None and desc_el.text else ""

        optimized = build_unique_description(
            title=title,
            brand=brand,
            product_type=product_type,
            mpn=pid,
            gtin=gtin,
            xml_desc_raw=raw_desc,
            product_url=link
        )

        if desc_el is None:
            desc_el = etree.SubElement(item, "description")
        set_cdata_text(desc_el, optimized)

        # imagem principal em alta
        img_el = item.find(g + "image_link")
        if img_el is not None and img_el.text:
            img_el.text = force_high_res_cdn(img_el.text.strip())

        # imagens adicionais
        additional = item.findall(g + "additional_image_link")

        # se já vierem do XML, só arruma e filtra lixo
        if additional:
            urls = []
            for a in additional:
                if a.text:
                    u = force_high_res_cdn(a.text.strip())
                    if not is_bad_image(u):
                        urls.append(u)

            # dedup + limita
            fixed = []
            seen = set()
            for u in urls:
                if u.lower() in seen:
                    continue
                seen.add(u.lower())
                fixed.append(u)
            fixed = fixed[:MAX_ADDITIONAL_IMAGES]

            # recria
            for a in additional:
                item.remove(a)
            for u in fixed:
                new_a = etree.SubElement(item, g + "additional_image_link")
                new_a.text = u
        else:
            # fallback: pega da página do produto
            gallery = extract_gallery_images_from_product_page(link)

            main_img = img_el.text.strip() if img_el is not None and img_el.text else ""
            gallery = [u for u in gallery if u != main_img and not is_bad_image(u)]
            gallery = gallery[:MAX_ADDITIONAL_IMAGES]

            for u in gallery:
                new_a = etree.SubElement(item, g + "additional_image_link")
                new_a.text = u

    xml_bytes = etree.tostring(root, pretty_print=True, xml_declaration=True, encoding="UTF-8")
    with open(OUTPUT_FILE, "wb") as f:
        f.write(xml_bytes)

    print(f"Feed gerado: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()

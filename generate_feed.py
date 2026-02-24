import re
import json
import time
import html
from typing import List, Optional, Dict, Any, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from lxml import etree


# =========================
# CONFIG
# =========================
SOURCE_XML_URL = "https://www.pccold.com.br/xml/6301c/googlemerchant.xml"
OUTPUT_FILE = "google-merchant.xml"

REQUEST_TIMEOUT = 20
SLEEP_BETWEEN_PAGES_SEC = 0.25  # evita “martelar” o site

# Só manda imagens com pelo menos isso (no maior lado), depois de tentar upgrade
MIN_IMAGE_MAX_SIDE = 600

# Tamanhos “prováveis” do CDN (vamos testar em ordem)
PREFERRED_SIZES = [
    (1600, 1600),
    (1200, 1200),
    (1000, 1000),
    (800, 800),
    (600, 600),
]

HEADERS = {
    "User-Agent": "PcColdFeedBot/1.1 (+https://www.pccold.com.br)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# =========================
# TEXT CLEANING
# =========================
EMOJI_RE = re.compile(
    "["

    # Dingbats/símbolos (inclui ✅ U+2705)
    "\u2600-\u27BF"

    # Emojis e pictogramas
    "\U0001F300-\U0001FAFF"

    # Flags
    "\U0001F1E6-\U0001F1FF"

    # Variation selectors
    "\uFE0E-\uFE0F"

    "]",
    flags=re.UNICODE,
)

WHITESPACE_RE = re.compile(r"\s+")


def clean_description_to_plain_text(raw: str) -> str:
    if not raw:
        return ""

    raw = html.unescape(raw)
    soup = BeautifulSoup(raw, "html.parser")

    for tag in soup(["style", "script", "noscript"]):
        tag.decompose()

    text = soup.get_text(" ", strip=True)

    # Remove padrões típicos de CSS “sobrando” como texto
    text = re.sub(r"\.[a-zA-Z0-9_-]+\s*\{[^}]*\}", " ", text)  # .classe { ... }
    text = re.sub(r"[a-zA-Z-]+\s*:\s*[^;]+;", " ", text)      # propriedade: valor;

    text = EMOJI_RE.sub("", text)
    text = WHITESPACE_RE.sub(" ", text).strip()
    return text


# =========================
# URL / IMAGE HELPERS
# =========================
SIZE_SEGMENT_RE = re.compile(r"/(\d{2,4})x(\d{2,4})/")

def normalize_url(u: str) -> str:
    if not u:
        return ""
    u = u.strip()
    if u.startswith("//"):
        u = "https:" + u
    return u

def is_valid_http_url(u: str) -> bool:
    try:
        p = urlparse(u)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False

def parse_size_from_url(u: str) -> Optional[Tuple[int, int]]:
    m = SIZE_SEGMENT_RE.search(u)
    if not m:
        return None
    try:
        return int(m.group(1)), int(m.group(2))
    except Exception:
        return None

def replace_size_in_url(u: str, w: int, h: int) -> str:
    return SIZE_SEGMENT_RE.sub(f"/{w}x{h}/", u, count=1)

def head_exists(url: str, session: requests.Session) -> bool:
    try:
        r = session.head(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        return r.status_code == 200
    except Exception:
        return False

def try_upgrade_awsli_image_url(u: str, session: requests.Session) -> str:
    """
    Se for URL com /<WxH>/ tenta trocar pra tamanhos maiores (1600/1200/1000/800...).
    Usa HEAD pra confirmar qual existe.
    """
    u = normalize_url(u)
    if "cdn.awsli.com.br" not in u:
        return u

    size = parse_size_from_url(u)
    if not size:
        return u

    cur_w, cur_h = size
    # Se já é grande, mantém
    if max(cur_w, cur_h) >= 1000:
        return u

    # 1) tentar tamanhos quadrados padrão
    for w, h in PREFERRED_SIZES:
        candidate = replace_size_in_url(u, w, h)
        if candidate != u and head_exists(candidate, session):
            return candidate

    # 2) fallback: tentar manter proporção (dobrar até bater 1200 no maior lado)
    max_side = max(cur_w, cur_h)
    if max_side > 0:
        scale = 1200 / max_side
        new_w = max(1, int(round(cur_w * scale)))
        new_h = max(1, int(round(cur_h * scale)))
        candidate = replace_size_in_url(u, new_w, new_h)
        if candidate != u and head_exists(candidate, session):
            return candidate

    return u

def unique_preserve_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for x in items:
        x = normalize_url(x)
        if not x:
            continue
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

def filter_small_images(urls: List[str]) -> List[str]:
    """
    Remove URLs que continuam pequenas (ex: /64x50/ /300x300/) após upgrade.
    Se não conseguir ler tamanho, mantém.
    """
    out = []
    for u in urls:
        sz = parse_size_from_url(u)
        if not sz:
            out.append(u)
            continue
        w, h = sz
        if max(w, h) >= MIN_IMAGE_MAX_SIDE:
            out.append(u)
    return out


# =========================
# PRODUCT PAGE PARSING
# =========================
def extract_jsonld_objects(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    objs = []
    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            content = s.get_text(strip=True)
            if not content:
                continue
            data = json.loads(content)
            if isinstance(data, list):
                for d in data:
                    if isinstance(d, dict):
                        objs.append(d)
            elif isinstance(data, dict):
                objs.append(data)
        except Exception:
            continue
    return objs

def find_product_jsonld(objs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for obj in objs:
        if obj.get("@type") == "Product":
            return obj
        graph = obj.get("@graph")
        if isinstance(graph, list):
            for g in graph:
                if isinstance(g, dict) and g.get("@type") == "Product":
                    return g
    return None

def extract_images_from_product_page(url: str, session: requests.Session) -> List[str]:
    try:
        resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    images: List[str] = []

    # 1) JSON-LD
    jsonld_objs = extract_jsonld_objects(soup)
    product = find_product_jsonld(jsonld_objs)
    if product:
        img_field = product.get("image")
        if isinstance(img_field, str):
            images.append(img_field)
        elif isinstance(img_field, list):
            for it in img_field:
                if isinstance(it, str):
                    images.append(it)

    # 2) Fallback HTML <img> (galeria)
    if len(images) < 2:
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-lazy") or ""
            src = normalize_url(src)
            if not src:
                continue
            if "cdn.awsli.com.br" in src and "/produto/" in src:
                images.append(src)

    images = unique_preserve_order(images)

    # Upgrade resolução + filtra thumbs
    upgraded = []
    for u in images:
        if is_valid_http_url(u):
            upgraded.append(try_upgrade_awsli_image_url(u, session))
    upgraded = unique_preserve_order(upgraded)
    upgraded = filter_small_images(upgraded)

    return upgraded

def extract_gtin_from_product_page(url: str, session: requests.Session) -> str:
    try:
        resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception:
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")
    jsonld_objs = extract_jsonld_objects(soup)
    product = find_product_jsonld(jsonld_objs)
    if not product:
        return ""

    for k in ("gtin13", "gtin", "gtin14", "gtin12", "gtin8"):
        v = product.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


# =========================
# XML FEED BUILDING
# =========================
def get_text(node: Optional[etree._Element]) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.strip()

def build_output_feed(source_tree: etree._ElementTree, session: requests.Session) -> bytes:
    nsmap = {"g": "http://base.google.com/ns/1.0"}

    source_root = source_tree.getroot()
    channel = source_root.find("channel")
    if channel is None:
        raise RuntimeError("XML de origem sem <channel>.")

    out_root = etree.Element("rss", version="2.0", nsmap=nsmap)
    out_channel = etree.SubElement(out_root, "channel")

    title = channel.findtext("title") or "PcCold Produtos"
    link = channel.findtext("link") or "https://www.pccold.com.br/"
    desc = channel.findtext("description") or "Feed PcCold Google Merchant"

    etree.SubElement(out_channel, "title").text = title
    etree.SubElement(out_channel, "link").text = link
    etree.SubElement(out_channel, "description").text = desc

    def gtag(name: str) -> str:
        return f"{{{nsmap['g']}}}{name}"

    for item in channel.findall("item"):
        out_item = etree.SubElement(out_channel, "item")

        src_id = item.find(gtag("id"))
        src_title = item.find("title")
        src_link = item.find("link")
        src_desc = item.find("description")

        sku = get_text(src_id)
        title_txt = get_text(src_title)
        link_txt = get_text(src_link)

        # description limpa
        raw_desc = get_text(src_desc)
        clean_desc = clean_description_to_plain_text(raw_desc)

        # imagem principal do XML (pode ser 800x800)
        src_img = item.find(gtag("image_link"))
        base_img = get_text(src_img)

        # Scrape imagens adicionais (e já faz upgrade/filtro)
        images = []
        if link_txt:
            time.sleep(SLEEP_BETWEEN_PAGES_SEC)
            images.extend(extract_images_from_product_page(link_txt, session))

        # garante que a imagem do XML também entra, mas tenta upgrade nela também
        if base_img:
            base_img_up = try_upgrade_awsli_image_url(base_img, session)
            images.insert(0, base_img_up)

        images = unique_preserve_order(images)
        images = filter_small_images(images)

        main_image = images[0] if images else ""
        additional_images = images[1:] if len(images) > 1 else []

        # GTIN/EAN
        src_gtin = item.find(gtag("gtin"))
        gtin = get_text(src_gtin)
        if not gtin and link_txt:
            time.sleep(SLEEP_BETWEEN_PAGES_SEC)
            gtin = extract_gtin_from_product_page(link_txt, session)

        # Outros campos
        brand = get_text(item.find(gtag("brand")))
        price = get_text(item.find(gtag("price")))
        sale_price = get_text(item.find(gtag("sale_price")))
        availability = get_text(item.find(gtag("availability"))) or "in stock"
        condition = get_text(item.find(gtag("condition"))) or "new"
        product_type = get_text(item.find(gtag("product_type")))

        # OUTPUT ITEM
        etree.SubElement(out_item, gtag("id")).text = sku
        etree.SubElement(out_item, "title").text = title_txt
        etree.SubElement(out_item, "link").text = link_txt
        etree.SubElement(out_item, "description").text = clean_desc

        if main_image:
            etree.SubElement(out_item, gtag("image_link")).text = main_image

        for u in additional_images[:10]:
            etree.SubElement(out_item, gtag("additional_image_link")).text = u

        etree.SubElement(out_item, gtag("availability")).text = availability
        etree.SubElement(out_item, gtag("condition")).text = condition

        if price:
            etree.SubElement(out_item, gtag("price")).text = price
        if sale_price:
            etree.SubElement(out_item, gtag("sale_price")).text = sale_price
        if brand:
            etree.SubElement(out_item, gtag("brand")).text = brand

        # MPN = SKU (seu padrão)
        if sku:
            etree.SubElement(out_item, gtag("mpn")).text = sku

        if gtin:
            etree.SubElement(out_item, gtag("gtin")).text = gtin

        if product_type:
            etree.SubElement(out_item, gtag("product_type")).text = product_type

    return etree.tostring(
        out_root,
        encoding="UTF-8",
        xml_declaration=True,
        pretty_print=True,
    )

def main() -> None:
    session = requests.Session()

    resp = session.get(SOURCE_XML_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    parser = etree.XMLParser(recover=True)
    source_root = etree.fromstring(resp.content, parser=parser)
    source_etree = etree.ElementTree(source_root)

    output_bytes = build_output_feed(source_etree, session)

    with open(OUTPUT_FILE, "wb") as f:
        f.write(output_bytes)

    print(f"[OK] Feed gerado: {OUTPUT_FILE}")
    print(f"[OK] Fonte: {SOURCE_XML_URL}")

if __name__ == "__main__":
    main()

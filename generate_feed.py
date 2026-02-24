import re
import json
import time
import html
from typing import List, Optional, Dict, Any
from urllib.parse import urljoin, urlparse

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

HEADERS = {
    "User-Agent": "PcColdFeedBot/1.0 (+https://www.pccold.com.br)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# =========================
# TEXT CLEANING
# =========================
# Remove emojis (inclui ✅ e vários ranges comuns)
EMOJI_RE = re.compile(
    "["

    # Dingbats, símbolos comuns (inclui ✅ U+2705)
    "\u2600-\u27BF"

    # Emojis e pictogramas
    "\U0001F300-\U0001FAFF"

    # Símbolos variados
    "\U0001F000-\U0001F02F"
    "\U0001F0A0-\U0001F0FF"

    # Flags
    "\U0001F1E6-\U0001F1FF"

    # Variation selectors
    "\uFE0E-\uFE0F"

    "]",
    flags=re.UNICODE,
)

WHITESPACE_RE = re.compile(r"\s+")


def clean_description_to_plain_text(raw: str) -> str:
    """
    Converte HTML/CSS para texto puro:
    - remove <style>, <script>, tags HTML
    - remove emojis (inclui ✅)
    - normaliza espaços
    """
    if not raw:
        return ""

    raw = html.unescape(raw)

    # Tenta interpretar como HTML
    soup = BeautifulSoup(raw, "html.parser")

    # Remove style/script
    for tag in soup(["style", "script", "noscript"]):
        tag.decompose()

    text = soup.get_text(" ", strip=True)

    # Se o conteúdo vier "poluído" com CSS inline (ex: ".descricao-produto { ... }")
    # isso costuma aparecer como texto mesmo. Vamos reduzir isso removendo blocos típicos.
    # Remove trechos muito “CSS-like”
    text = re.sub(r"\.[a-zA-Z0-9_-]+\s*\{[^}]*\}", " ", text)  # .classe { ... }
    text = re.sub(r"[a-zA-Z-]+\s*:\s*[^;]+;", " ", text)      # propriedade: valor;

    # Remove emojis
    text = EMOJI_RE.sub("", text)

    # Normaliza whitespace
    text = WHITESPACE_RE.sub(" ", text).strip()

    return text


# =========================
# IMAGE HELPERS
# =========================
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


def try_upgrade_image_url(u: str, session: requests.Session) -> str:
    """
    Tenta melhorar resolução quando o CDN usa /800x800/.
    - tenta /1200x1200/
    - se não existir, mantém original
    """
    u = normalize_url(u)
    if "/800x800/" not in u:
        return u

    candidate = u.replace("/800x800/", "/1200x1200/")

    try:
        # HEAD é mais leve; se falhar, cai no original
        r = session.head(candidate, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if r.status_code == 200:
            return candidate
    except Exception:
        pass

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
    # Procura objeto Product, inclusive dentro de @graph
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
    """
    Retorna lista de URLs de imagem (principal + adicionais) da página do produto.
    Prioridade: JSON-LD -> HTML <img>.
    """
    try:
        resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # 1) JSON-LD
    jsonld_objs = extract_jsonld_objects(soup)
    product = find_product_jsonld(jsonld_objs)
    images: List[str] = []

    if product:
        img_field = product.get("image")
        if isinstance(img_field, str):
            images.append(img_field)
        elif isinstance(img_field, list):
            for it in img_field:
                if isinstance(it, str):
                    images.append(it)

    # 2) Fallback HTML: pegar imagens que parecem ser da galeria
    if len(images) < 2:
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-lazy") or ""
            src = normalize_url(src)
            if not src:
                continue

            # Heurística: CDN da Loja Integrada / awsli / e “produto”
            if "cdn.awsli.com.br" in src and "/produto/" in src:
                images.append(src)

    images = unique_preserve_order(images)

    # Tenta subir resolução quando possível
    upgraded = []
    for u in images:
        if is_valid_http_url(u):
            upgraded.append(try_upgrade_image_url(u, session))
    return unique_preserve_order(upgraded)


def extract_gtin_from_product_page(url: str, session: requests.Session) -> str:
    """
    Tenta achar GTIN (EAN) no JSON-LD da página.
    """
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

    # Campos comuns
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
    nsmap = {
        "g": "http://base.google.com/ns/1.0",
    }

    source_root = source_tree.getroot()

    # Procura channel
    channel = source_root.find("channel")
    if channel is None:
        raise RuntimeError("XML de origem sem <channel>.")

    out_root = etree.Element("rss", version="2.0", nsmap=nsmap)
    out_channel = etree.SubElement(out_root, "channel")

    # channel basic
    title = channel.findtext("title") or "PcCold Produtos"
    link = channel.findtext("link") or "https://www.pccold.com.br/"
    desc = channel.findtext("description") or "Feed PcCold Google Merchant"

    etree.SubElement(out_channel, "title").text = title
    etree.SubElement(out_channel, "link").text = link
    etree.SubElement(out_channel, "description").text = desc

    # namespace helper
    def gtag(name: str) -> str:
        return f"{{{nsmap['g']}}}{name}"

    # Itera itens
    for item in channel.findall("item"):
        out_item = etree.SubElement(out_channel, "item")

        # Campos base
        src_id = item.find(gtag("id"))
        src_title = item.find("title")
        src_link = item.find("link")
        src_desc = item.find("description")

        sku = get_text(src_id)
        title_txt = get_text(src_title)
        link_txt = get_text(src_link)

        # description limpa (texto puro)
        raw_desc = get_text(src_desc)
        clean_desc = clean_description_to_plain_text(raw_desc)

        # Imagens: base + adicionais por scraping
        src_img = item.find(gtag("image_link"))
        base_img = get_text(src_img)

        images = []
        if link_txt:
            time.sleep(SLEEP_BETWEEN_PAGES_SEC)
            page_images = extract_images_from_product_page(link_txt, session)
            images.extend(page_images)

        # garante que a imagem do XML também entra (caso scraping falhe)
        if base_img:
            images.insert(0, base_img)

        images = unique_preserve_order(images)

        main_image = images[0] if images else base_img
        additional_images = images[1:] if len(images) > 1 else []

        # GTIN/EAN
        src_gtin = item.find(gtag("gtin"))
        gtin = get_text(src_gtin)

        if not gtin and link_txt:
            time.sleep(SLEEP_BETWEEN_PAGES_SEC)
            gtin = extract_gtin_from_product_page(link_txt, session)

        # Outros campos úteis
        src_brand = item.find(gtag("brand"))
        brand = get_text(src_brand)

        src_price = item.find(gtag("price"))
        price = get_text(src_price)

        src_sale_price = item.find(gtag("sale_price"))
        sale_price = get_text(src_sale_price)

        src_avail = item.find(gtag("availability"))
        availability = get_text(src_avail) or "in stock"

        src_cond = item.find(gtag("condition"))
        condition = get_text(src_cond) or "new"

        src_product_type = item.find(gtag("product_type"))
        product_type = get_text(src_product_type)

        # =========================
        # OUTPUT ITEM
        # =========================
        etree.SubElement(out_item, gtag("id")).text = sku
        etree.SubElement(out_item, "title").text = title_txt
        etree.SubElement(out_item, "link").text = link_txt
        etree.SubElement(out_item, "description").text = clean_desc

        if main_image:
            etree.SubElement(out_item, gtag("image_link")).text = main_image

        for u in additional_images[:10]:  # Google aceita várias, mas vamos limitar
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

    # Bytes final
    return etree.tostring(
        out_root,
        encoding="UTF-8",
        xml_declaration=True,
        pretty_print=True,
    )


def main() -> None:
    session = requests.Session()

    # Baixa XML origem
    resp = session.get(SOURCE_XML_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    parser = etree.XMLParser(recover=True)
    source_tree = etree.fromstring(resp.content, parser=parser)
    source_etree = etree.ElementTree(source_tree)

    output_bytes = build_output_feed(source_etree, session)

    with open(OUTPUT_FILE, "wb") as f:
        f.write(output_bytes)

    print(f"[OK] Feed gerado: {OUTPUT_FILE}")
    print(f"[OK] Fonte: {SOURCE_XML_URL}")


if __name__ == "__main__":
    main()

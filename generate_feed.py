import re
import sys
import html
import datetime
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET


# =========================================================
# CONFIG
# =========================================================
BASE_FEED_URL = "https://www.pccold.com.br/xml/6301c/googlemerchant.xml"

OUTPUT_XML_PATH = "google-merchant.xml"

# Merchant limits: descrição pode ser grande, mas é melhor manter “limpa” e objetiva.
# Se você quiser, aumente para 1500/2000. Eu deixei 1200 pra ficar bem “Shopping-friendly”.
MAX_DESCRIPTION_CHARS = 1200

# Quantidade máxima de imagens adicionais por item (Google aceita várias, mas não precisa exagerar)
MAX_ADDITIONAL_IMAGES = 10

# Timeout e user-agent pra evitar bloqueio bobo
HTTP_TIMEOUT = 30
HEADERS = {
    "User-Agent": "PcColdFeedBot/1.0 (+https://www.pccold.com.br)"
}

# Namespace do Google Merchant
G_NS = "http://base.google.com/ns/1.0"
NS = {"g": G_NS}


# =========================================================
# HELPERS
# =========================================================
def normalize_whitespace(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_description_html_to_text(description_html: str) -> str:
    """
    Recebe HTML da descrição do produto (do XML da Loja Integrada),
    remove CSS/estilos/scripts e transforma em texto limpo.
    Não inventa conteúdo.
    """
    if not description_html:
        return ""

    # Decode entidades HTML e garante string
    description_html = html.unescape(description_html)

    soup = BeautifulSoup(description_html, "lxml")

    # Remove lixo comum
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    # Remove atributos de estilo (pra não “vazar” CSS no texto)
    for tag in soup.find_all(True):
        if tag.has_attr("style"):
            del tag["style"]
        if tag.has_attr("class"):
            del tag["class"]

    # Texto final: preserva quebras em itens de lista e headings
    # Convertendo <li> em “- item”
    for li in soup.find_all("li"):
        li.insert_before("\n- ")
        li.append("\n")

    # Quebras em headings
    for hx in soup.find_all(["h1", "h2", "h3", "h4"]):
        hx.insert_before("\n")
        hx.append("\n")

    text = soup.get_text(separator=" ", strip=True)
    text = normalize_whitespace(text)

    # Corta no tamanho desejado sem “quebrar” no meio de palavra
    if len(text) > MAX_DESCRIPTION_CHARS:
        cut = text[:MAX_DESCRIPTION_CHARS]
        # corta no último espaço
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        text = cut.strip()

    return text


def force_cdn_size_1600(url: str) -> str:
    """
    Converte URLs do CDN da Loja Integrada (awsli) para /1600x1600/ quando possível.
    Ex.: https://cdn.awsli.com.br/800x800/... -> https://cdn.awsli.com.br/1600x1600/...
    Também cobre casos 300x300, 600x1000, 64x50 etc.
    """
    if not url:
        return url

    u = url.strip()
    # Se já é 1600x1600, ok
    if "/1600x1600/" in u:
        return u

    # Troca qualquer /{num}x{num}/ por /1600x1600/
    u2 = re.sub(r"/\d{2,4}x\d{2,4}/", "/1600x1600/", u)
    return u2


def strip_utm(url: str) -> str:
    """
    Opcional: remove utm_ do link. (Se você quiser manter, pode comentar.)
    """
    if not url:
        return url
    try:
        parts = urlparse(url)
        if not parts.query:
            return url
        # remove apenas utm_*
        q = parts.query.split("&")
        q2 = [x for x in q if not x.lower().startswith("utm_")]
        new_query = "&".join(q2)
        return urlunparse((parts.scheme, parts.netloc, parts.path, parts.params, new_query, parts.fragment))
    except Exception:
        return url


def fetch_url(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.text


def extract_images_from_product_page(product_url: str) -> list[str]:
    """
    Puxa imagens da página do produto.
    Heurística:
      - pega <img> com src/data-src que contenha cdn.awsli
      - evita ícones/pix/logos e thumbnails muito “genéricos”
      - dedup
      - força 1600x1600
    """
    if not product_url:
        return []

    html_page = fetch_url(product_url)
    soup = BeautifulSoup(html_page, "lxml")

    candidates = []

    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if not src:
            continue

        src = src.strip()

        # Só CDN awsli
        if "cdn.awsli.com.br" not in src:
            continue

        # Ignora coisas comuns que não são foto do produto
        bad_markers = [
            "logo", "pix", "paghiper", "site-seguro",
            "produto-sem-imagem", "stamp_google_safe_browsing",
            "/static/img/", "/production/static/img/"
        ]
        if any(b.lower() in src.lower() for b in bad_markers):
            continue

        candidates.append(force_cdn_size_1600(src))

    # Dedup mantendo ordem
    seen = set()
    images = []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            images.append(u)

    # Limita
    return images[:MAX_ADDITIONAL_IMAGES]


def ensure_cdata(text: str) -> str:
    """
    ElementTree não cria CDATA automaticamente.
    Aqui a gente só garante que o texto esteja limpo.
    (O Merchant aceita sem CDATA; CDATA é opcional.)
    """
    if text is None:
        return ""
    return text


def g_find_text(item: ET.Element, tag: str) -> str:
    el = item.find(f"g:{tag}", NS)
    return el.text.strip() if el is not None and el.text else ""


def g_set_text(item: ET.Element, tag: str, value: str) -> None:
    el = item.find(f"g:{tag}", NS)
    if el is None:
        el = ET.SubElement(item, f"{{{G_NS}}}{tag}")
    el.text = value


def g_remove_all(item: ET.Element, tag: str) -> None:
    for el in list(item.findall(f"g:{tag}", NS)):
        item.remove(el)


# =========================================================
# MAIN
# =========================================================
def main():
    print(f"[INFO] Fetching base feed: {BASE_FEED_URL}")
    base_xml = fetch_url(BASE_FEED_URL)

    # Parse do XML com namespace
    try:
        root = ET.fromstring(base_xml)
    except Exception as e:
        print("[ERROR] Failed to parse base XML:", e)
        sys.exit(1)

    # Garante que o namespace g está registrado pra saída
    ET.register_namespace("g", G_NS)

    channel = root.find("channel")
    if channel is None:
        print("[ERROR] channel not found in XML.")
        sys.exit(1)

    items = channel.findall("item")
    print(f"[INFO] Items found: {len(items)}")

    updated = 0
    for item in items:
        # Campos principais
        prod_link = item.findtext("link", default="").strip()
        title = item.findtext("title", default="").strip()

        # Se quiser remover UTM do link, liga aqui:
        if prod_link:
            prod_link = strip_utm(prod_link)
            # grava de volta no <link>
            link_el = item.find("link")
            if link_el is None:
                link_el = ET.SubElement(item, "link")
            link_el.text = prod_link

        # 1) DESCRIÇÃO: pega do <description> do item (HTML), limpa e coloca texto limpo
        desc_el = item.find("description")
        desc_html = desc_el.text if desc_el is not None and desc_el.text else ""
        desc_clean = clean_description_html_to_text(desc_html)

        # Se por algum motivo vier vazio, usa o título como fallback
        if not desc_clean:
            desc_clean = title

        if desc_el is None:
            desc_el = ET.SubElement(item, "description")
        desc_el.text = ensure_cdata(desc_clean)

        # 2) IMAGEM PRINCIPAL: força 1600x1600 se for awsli
        img_main = g_find_text(item, "image_link")
        if img_main:
            g_set_text(item, "image_link", force_cdn_size_1600(img_main))

        # 3) IMAGENS ADICIONAIS: gera a partir da página do produto
        # Remove todas as <g:additional_image_link> atuais e recria
        g_remove_all(item, "additional_image_link")

        # Puxa imagens da página e coloca como adicionais
        additional_images = []
        try:
            additional_images = extract_images_from_product_page(prod_link)
        except Exception as e:
            print(f"[WARN] Failed to extract images for: {prod_link} -> {e}")
            additional_images = []

        # Evita repetir a principal nas adicionais
        main_1600 = force_cdn_size_1600(img_main) if img_main else ""
        filtered = []
        for u in additional_images:
            if u and u != main_1600:
                filtered.append(u)

        # Grava adicionais
        for u in filtered[:MAX_ADDITIONAL_IMAGES]:
            g_set_text(item, "additional_image_link", u)

        # 4) GARANTE MPN = SKU (você falou que SKU = MPN)
        # No seu feed, normalmente <g:mpn> já vem. Se vier vazio, copia do g:id ou do código do título.
        mpn = g_find_text(item, "mpn")
        gid = g_find_text(item, "id")

        if not mpn and gid:
            g_set_text(item, "mpn", gid)

        # 5) GTIN/EAN: se já existe <g:gtin>, mantém.
        # (Não inventa. Se não vier no XML base, fica vazio.)
        gtin = g_find_text(item, "gtin")
        if gtin:
            g_set_text(item, "gtin", gtin)

        updated += 1
        if updated % 25 == 0:
            print(f"[INFO] Updated {updated}/{len(items)} ...")

    # Atualiza timestamp no título/descrição do channel se quiser (opcional)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ch_desc = channel.find("description")
    if ch_desc is not None and ch_desc.text:
        ch_desc.text = f"{ch_desc.text} | Atualizado: {now}"

    # Salva
    tree = ET.ElementTree(root)
    tree.write(OUTPUT_XML_PATH, encoding="utf-8", xml_declaration=True)

    print(f"[OK] Generated: {OUTPUT_XML_PATH} with {len(items)} items.")


if __name__ == "__main__":
    main()

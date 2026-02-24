import re
import sys
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from lxml import etree

# =========================
# CONFIG
# =========================
BASE_XML_URL = "https://www.pccold.com.br/xml/6301c/googlemerchant.xml"
OUTPUT_XML_PATH = "google-merchant.xml"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 25
SLEEP_BETWEEN_PRODUCTS_SEC = 0.25

MAX_ADDITIONAL_IMAGES = 10  # Google aceita várias, mas não precisa exagerar

# Namespace do Google Merchant
G_NS = "http://base.google.com/ns/1.0"
NSMAP = {"g": G_NS}


# =========================
# HTTP
# =========================
def http_get(url: str) -> requests.Response:
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r


# =========================
# HELPERS: XML
# =========================
def gtag(tag_name: str) -> str:
    """Retorna '{namespace}tag' para usar no lxml."""
    return f"{{{G_NS}}}{tag_name}"


def get_text(el):
    return (el.text or "").strip() if el is not None else ""


def set_or_create(item_el, tag_name: str, value: str):
    """
    Seta texto em <g:tag_name>. Se não existir, cria.
    """
    child = item_el.find(gtag(tag_name))
    if child is None:
        child = etree.SubElement(item_el, gtag(tag_name))
    child.text = value


def remove_all(item_el, tag_name: str):
    """
    Remove todas as ocorrências de <g:tag_name> dentro de item_el.
    """
    for child in item_el.findall(gtag(tag_name)):
        item_el.remove(child)


# =========================
# HELPERS: URL / IMAGES
# =========================
SIZE_SEGMENT_RE = re.compile(r"/(\d{2,4}x\d{2,4})/")
PRODUCT_ID_RE = re.compile(r"/produto/(\d+)/")

def to_high_res(url: str, size: str = "1600x1600") -> str:
    """
    Converte URLs do CDN da Loja Integrada para alta.
    Ex: https://cdn.awsli.com.br/64x50/... -> /1600x1600/...
    """
    return SIZE_SEGMENT_RE.sub(f"/{size}/", url)

def extract_product_id_from_cdn_url(url: str) -> str:
    m = PRODUCT_ID_RE.search(url)
    return m.group(1) if m else ""


def is_valid_cdn_product_image(url: str) -> bool:
    if "cdn.awsli.com.br" not in url:
        return False
    if "/produto/" not in url:
        return False

    # Lixos comuns
    bad_parts = [
        "produto-sem-imagem.gif",
        "/production/static/",
        "/struct/",
        "/static/",
        "/logo",
        "/logo-",
        "selo-",
        "rodap",
        "paghiper",
        "pix",
    ]
    u = url.lower()
    if any(p in u for p in bad_parts):
        return False

    return True


def unique_keep_order(seq):
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# =========================
# SCRAPE: DESCRIPTION + IMAGES
# =========================
def extract_description_block_from_product_page(html: str) -> str:
    """
    Pega somente o texto do bloco de descrição do produto,
    evitando preço/parcelas/pix/carrinho.
    Estratégia:
      - pega o H1 do produto e coleta textos logo após,
        até antes de "Produtos relacionados" (ou rodapé).
    """
    soup = BeautifulSoup(html, "lxml")

    # Remove coisas que poluem texto
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    h1 = soup.find("h1")
    if not h1:
        return ""

    # Vamos caminhar pelos elementos depois do H1
    texts = []
    stop_markers = [
        "Produtos relacionados",
        "Sobre a loja",
        "Cadastre-se e Ganhe",
        "Política",
        "Trocas e Devoluções",
    ]

    # Em Loja Integrada, muitas vezes a descrição vem em H2/H3 + listas + tabelas.
    # Vamos coletar textos de tags comuns até bater num marcador.
    current = h1
    steps = 0
    while current and steps < 250:
        current = current.find_next()
        steps += 1
        if not current:
            break

        if current.name in ("h1",):
            continue

        # Se chegou em rodapé/relacionados, para
        t = current.get_text(" ", strip=True)
        if t:
            if any(m.lower() in t.lower() for m in stop_markers):
                break

        # Coleta apenas tags "de conteúdo"
        if current.name in ("h2", "h3", "h4", "p", "li", "td", "th"):
            if t:
                texts.append(t)

    raw = "\n".join(texts).strip()
    return raw


PARCELAS_RE = re.compile(r"\b\d+x\s+de\s+R\$\s*[\d\.,]+\s*sem\s+juros\b", re.IGNORECASE)
PIX_RE = re.compile(r"\bou\s+R\$\s*[\d\.,]+\s*via\s+Pix\b", re.IGNORECASE)
CURRENCY_LINE_RE = re.compile(r"R\$\s*[\d\.,]+")
EXCESS_SPACES_RE = re.compile(r"\s+")

def clean_description(text: str) -> str:
    """
    Limpa:
      - parcelas (1x/2x/3x)
      - "via Pix"
      - linhas de preço soltas
      - restos de navegação
    Mantém só texto útil do produto.
    """
    if not text:
        return ""

    # Remove linhas inteiras que pareçam preço/pagamento
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    kept = []
    for ln in lines:
        if PARCELAS_RE.search(ln):
            continue
        if PIX_RE.search(ln):
            continue

        # Se a linha é basicamente preço puro, remove
        if len(ln) <= 18 and CURRENCY_LINE_RE.search(ln):
            continue

        # remove "Calcule o frete" e botões
        low = ln.lower()
        if "calcule o frete" in low or "finalizar compra" in low or "estoque:" in low:
            continue

        kept.append(ln)

    out = " ".join(kept)
    out = EXCESS_SPACES_RE.sub(" ", out).strip()

    # Tamanho seguro pro Merchant (limite é alto, mas vamos manter “enxuto”)
    return out[:4500]


def extract_gallery_images_from_product_page(html: str) -> list[str]:
    """
    Extrai imagens do produto SEM misturar com relacionados.
    Regra principal:
      - pega URLs do CDN que tenham /produto/{ID}/
      - descobre o ID do produto pela 1ª imagem válida
      - mantém somente URLs com o mesmo ID
      - converte tudo pra 1600x1600
    """
    soup = BeautifulSoup(html, "lxml")

    candidates = []

    # img src
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        if src:
            candidates.append(src)

    # a href (às vezes a galeria usa links)
    for a in soup.find_all("a"):
        href = a.get("href") or ""
        if href:
            candidates.append(href)

    # Filtra candidatos válidos
    candidates = [c for c in candidates if is_valid_cdn_product_image(c)]

    # Descobre o product_id
    product_id = ""
    for c in candidates:
        pid = extract_product_id_from_cdn_url(c)
        if pid:
            product_id = pid
            break

    if not product_id:
        return []

    # Mantém somente imagens do mesmo produto
    same_product = []
    for c in candidates:
        if extract_product_id_from_cdn_url(c) == product_id:
            same_product.append(c)

    # Sobe resolução e remove duplicadas
    hi = [to_high_res(u, "1600x1600") for u in same_product]
    hi = unique_keep_order(hi)

    return hi


# =========================
# MAIN PIPELINE
# =========================
def build_optimized_description(item_el, product_html: str) -> str:
    """
    Gera uma descrição "otimizada com segurança":
    - baseada no texto REAL do produto (sem inventar)
    - inclui identificadores importantes do item (Marca/MPN/GTIN/SKU)
    """
    page_desc = extract_description_block_from_product_page(product_html)
    page_desc = clean_description(page_desc)

    # Identificadores do XML base
    title = get_text(item_el.find("title"))
    brand = get_text(item_el.find(gtag("brand")))
    mpn = get_text(item_el.find(gtag("mpn")))
    gtin = get_text(item_el.find(gtag("gtin")))
    sku = get_text(item_el.find(gtag("id")))

    # “Cabeçalho” curto, sem exagero (e sem emoji)
    header_parts = []
    if title:
        header_parts.append(title)
    if brand:
        header_parts.append(f"Marca: {brand}")
    if sku:
        header_parts.append(f"SKU: {sku}")
    if mpn:
        header_parts.append(f"MPN: {mpn}")
    if gtin:
        header_parts.append(f"EAN: {gtin}")

    header = " | ".join(header_parts)

    # Se a página não trouxe nada (raro), cai pro title + ids
    if not page_desc:
        return header[:4500]

    # Junta de forma limpa
    final = f"{header}. {page_desc}"
    final = EXCESS_SPACES_RE.sub(" ", final).strip()
    return final[:4500]


def process_feed():
    # 1) Baixa XML base
    base_xml = http_get(BASE_XML_URL).content

    # 2) Parse
    parser = etree.XMLParser(remove_blank_text=True, recover=True)
    root = etree.fromstring(base_xml, parser=parser)

    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("XML base inválido: não encontrei <channel>.")

    items = channel.findall("item")
    print(f"[INFO] Itens no XML base: {len(items)}")

    # 3) Processa cada item
    for idx, item in enumerate(items, start=1):
        link_el = item.find("link")
        if link_el is None or not (link_el.text or "").strip():
            continue

        product_url = (link_el.text or "").strip()

        try:
            html = http_get(product_url).text
        except Exception as e:
            print(f"[WARN] Falha ao abrir produto ({product_url}): {e}")
            continue

        # 3a) Descrição limpa/otimizada (sem inventar e sem parcelas/pix)
        optimized_desc = build_optimized_description(item, html)
        set_or_create(item, "description", optimized_desc)

        # 3b) Imagens da galeria (sem misturar com relacionados)
        images = extract_gallery_images_from_product_page(html)

        if images:
            # image_link principal
            set_or_create(item, "image_link", images[0])

            # additional_image_link
            remove_all(item, "additional_image_link")

            extras = images[1: 1 + MAX_ADDITIONAL_IMAGES]
            for u in extras:
                etree.SubElement(item, gtag("additional_image_link")).text = u

        # 3c) Pequena pausa pra não “martelar” o site
        if idx % 20 == 0:
            print(f"[INFO] Processados {idx}/{len(items)}...")
        time.sleep(SLEEP_BETWEEN_PRODUCTS_SEC)

    # 4) Atualiza descrição do channel com timestamp (opcional)
    ch_desc = channel.find("description")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if ch_desc is not None and ch_desc.text:
        base = ch_desc.text.strip()
        ch_desc.text = f"{base} | Atualizado: {now}"
    else:
        etree.SubElement(channel, "description").text = f"Feed PcCold | Atualizado: {now}"

    # 5) Salva
    xml_bytes = etree.tostring(
        root,
        pretty_print=True,
        xml_declaration=True,
        encoding="UTF-8",
    )
    with open(OUTPUT_XML_PATH, "wb") as f:
        f.write(xml_bytes)

    print(f"[OK] Feed gerado em: {OUTPUT_XML_PATH}")


if __name__ == "__main__":
    try:
        process_feed()
    except Exception as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)

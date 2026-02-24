# generate_feed.py
# Gera google-merchant.xml a partir do XML base (Loja Integrada),
# enriquecendo com:
# - descrição limpa (somente bloco de descrição do produto, sem parcelas/frete)
# - imagens adicionais (galeria do produto)
# - normalização de imagens para 1600x1600 quando possível
#
# Requer: requests, beautifulsoup4, lxml

from __future__ import annotations

import re
import sys
import html
from datetime import datetime, timezone
from typing import List, Optional, Tuple, Set
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from lxml import etree

# =========================
# CONFIGURAÇÕES
# =========================

BASE_FEED_URL = "https://www.pccold.com.br/xml/6301c/googlemerchant.xml"

OUTPUT_FILE = "google-merchant.xml"

USER_AGENT = "Mozilla/5.0 (PcColdFeedBot/1.0; +https://pccold-systems.github.io/pccold-feed/)"

REQUEST_TIMEOUT = 30

# Limite razoável de descrição para Merchant (ele aceita mais, mas evitar textos gigantes ajuda)
MAX_DESCRIPTION_CHARS = 2000

# Quantidade máxima de imagens adicionais (Google aceita várias; manter controlado ajuda)
MAX_ADDITIONAL_IMAGES = 10

# Normalização de tamanho no CDN (quando existir /800x800/ etc)
TARGET_IMG_SIZE = "1600x1600"

# =========================
# HELPERS
# =========================

def http_get(url: str) -> str:
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.text


def normalize_url(u: str) -> str:
    """Remove espaços e normaliza encoding básico."""
    u = (u or "").strip()
    return u


_IMG_SIZE_RE = re.compile(r"/(\d{2,4}x\d{2,4})/")

def normalize_image_url(url: str) -> str:
    """
    Se for CDN com /{w}x{h}/, troca para /1600x1600/.
    Ex.: /800x800/ -> /1600x1600/
    """
    url = normalize_url(url)
    if not url:
        return url

    # Só faz isso no domínio do CDN que você mostrou
    if "cdn.awsl" in url:
        url = _IMG_SIZE_RE.sub(f"/{TARGET_IMG_SIZE}/", url)

    return url


def strip_emoji(s: str) -> str:
    # Remove emojis e símbolos fora do BMP de forma simples
    return re.sub(r"[\U00010000-\U0010FFFF]", "", s)


def clean_text(s: str) -> str:
    s = html.unescape(s or "")
    s = strip_emoji(s)
    # Remove CSS/trechos típicos
    s = re.sub(r"\{[^{}]*\}", " ", s)  # blocos tipo { ... }
    s = re.sub(r"\s+", " ", s).strip()
    return s


def looks_like_payment_or_ui_line(line: str) -> bool:
    l = line.lower().strip()
    if not l:
        return True

    # Padrões de UI/checkout/frete/parcelas
    bad_patterns = [
        "até", "sem juros", "via pix", "finalizar compra", "calcule o frete",
        "parcelas", "não sei meu cep", "estoque", "disponível",
        "r$", "paghiper", "pix", "ok"
    ]
    # Só marque como "bad" se tiver cara de UI/preço
    if any(p in l for p in bad_patterns):
        # mas não queremos apagar casos raros onde a descrição fala "pix" etc.
        # então exigimos evidência forte de preço/parcelamento
        if ("r$" in l) or re.search(r"\b\d+x\b", l) or ("sem juros" in l) or ("finalizar compra" in l):
            return True
        if ("calcule o frete" in l) or ("parcelas" in l):
            return True

    # Linhas só numéricas / fragmentos de preço
    if re.fullmatch(r"r?\$?\s*[\d\.,]+", l):
        return True

    return False


def limit_description(text: str, max_chars: int = MAX_DESCRIPTION_CHARS) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    # corta em limite e tenta terminar em ponto
    cut = text[:max_chars].rsplit(". ", 1)[0]
    if len(cut) < 200:
        cut = text[:max_chars]
    return cut.strip()


def soup_text_preserve_lines(node) -> List[str]:
    """
    Extrai texto com quebras (p/ manter bullets e seções),
    retornando lista de linhas.
    """
    raw = node.get_text("\n", strip=True)
    raw = html.unescape(raw)
    raw = strip_emoji(raw)

    lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw.split("\n")]
    # remove vazias e duplicadas seguidas
    out = []
    last = None
    for ln in lines:
        if not ln:
            continue
        if ln == last:
            continue
        out.append(ln)
        last = ln
    return out


def pick_description_container(soup: BeautifulSoup):
    """
    Tenta achar o container do bloco 'descrição do produto' (não preço/parcelas).
    Estratégia:
    1) procurar headings como "Principais Vantagens" e subir para uma seção
    2) procurar elementos com classes comuns de descrição
    3) fallback: pegar o maior bloco de texto após o H1 principal
    """
    # 1) por headings marcantes
    for heading_text in ["Principais Vantagens", "Ideal Para", "Inclui na Embalagem", "Especificações Técnicas"]:
        h = soup.find(lambda tag: tag.name in ["h2", "h3", "strong"] and tag.get_text(strip=True) == heading_text)
        if h:
            # sobe até achar um container razoável
            cur = h
            for _ in range(8):
                if not cur:
                    break
                if cur.name in ["section", "article", "div"]:
                    return cur
                cur = cur.parent

    # 2) por classes/ids típicos
    selectors = [
        {"id": "descricao"},
        {"id": "descricao-produto"},
        {"class_": re.compile(r"(descricao|description)", re.I)},
        {"class_": re.compile(r"(product|produto).*?(desc|descricao)", re.I)},
    ]
    for sel in selectors:
        node = soup.find(**sel)
        if node and len(node.get_text(strip=True)) > 120:
            return node

    # 3) fallback: maior bloco de texto em divs
    candidates = soup.find_all(["div", "section", "article"])
    best = None
    best_len = 0
    for c in candidates:
        t = c.get_text(" ", strip=True)
        if not t:
            continue
        # penaliza blocos com muito preço/parcelas
        low = t.lower()
        penalty = 0
        if "sem juros" in low or "finalizar compra" in low or "calcule o frete" in low:
            penalty = 500
        score = max(0, len(t) - penalty)
        if score > best_len:
            best_len = score
            best = c
    return best


def extract_best_description(product_url: str) -> str:
    """
    Busca página do produto e extrai um texto de descrição LIMPO e ÚNICO.
    """
    html_text = http_get(product_url)
    soup = BeautifulSoup(html_text, "lxml")

    # remove scripts/styles
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    container = pick_description_container(soup)
    if not container:
        return ""

    lines = soup_text_preserve_lines(container)

    # Filtra linhas ruins (parcelas, UI, preços)
    filtered = []
    for ln in lines:
        if looks_like_payment_or_ui_line(ln):
            continue
        filtered.append(ln)

    # Monta descrição com separadores bons
    # Se tiver bullets, mantém como "- ..."
    pretty_lines = []
    for ln in filtered:
        if ln.startswith(("•", "-", "–")):
            pretty_lines.append(f"- {ln.lstrip('•-– ').strip()}")
        else:
            pretty_lines.append(ln)

    # Remove repetições e cola
    text = "\n".join(pretty_lines).strip()

    # Se ficou enorme, dá uma limpada extra de ruído
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    text = limit_description(text, MAX_DESCRIPTION_CHARS)

    # Normaliza espaços e remove qualquer resto de CSS visível
    text = text.replace("  ", " ").strip()
    return text


def extract_gallery_images(product_url: str) -> List[str]:
    """
    Tenta extrair imagens da galeria da página do produto.
    Estratégia:
    - pega todas as URLs que pareçam imagem do CDN (cdn.awsl...) e que contenham /produto/
    - normaliza para 1600x1600
    - remove duplicadas e gifs de "produto sem imagem"
    """
    html_text = http_get(product_url)
    soup = BeautifulSoup(html_text, "lxml")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    urls: Set[str] = set()

    # pega <img src=...> e data-src/data-zoom etc
    for img in soup.find_all("img"):
        for attr in ["src", "data-src", "data-zoom-image", "data-large_image", "data-original"]:
            u = img.get(attr)
            if not u:
                continue
            u = normalize_url(u)
            if "cdn.awsl" not in u:
                continue
            if "/produto/" not in u:
                continue
            if "produto-sem-imagem" in u:
                continue
            urls.add(normalize_image_url(u))

    # também varre links
    for a in soup.find_all("a"):
        u = a.get("href")
        if not u:
            continue
        u = normalize_url(u)
        if "cdn.awsl" not in u:
            continue
        if "/produto/" not in u:
            continue
        if not re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", u, re.I):
            continue
        urls.add(normalize_image_url(u))

    # ordena mantendo consistência
    out = sorted(urls)
    return out[:MAX_ADDITIONAL_IMAGES]


def etree_text(tag, default: str = "") -> str:
    if tag is None or tag.text is None:
        return default
    return tag.text.strip()


def ns(tag: str) -> str:
    return f"{{http://base.google.com/ns/1.0}}{tag}"


def safe_add(parent, tag: str, text: str, use_cdata: bool = False, is_g: bool = False):
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return None

    qname = ns(tag) if is_g else tag
    el = etree.SubElement(parent, qname)
    if use_cdata:
        el.text = etree.CDATA(text)
    else:
        el.text = text
    return el


def build_output_feed(items_data: List[dict]) -> etree._ElementTree:
    rss = etree.Element("rss", version="2.0", nsmap={"g": "http://base.google.com/ns/1.0"})
    channel = etree.SubElement(rss, "channel")

    safe_add(channel, "title", "PcCold")
    safe_add(channel, "link", "https://www.pccold.com.br/")
    safe_add(channel, "description", f"Feed PcCold (atualizado em {datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")

    for d in items_data:
        item = etree.SubElement(channel, "item")
        safe_add(item, "title", d["title"], use_cdata=True)
        safe_add(item, "link", d["link"], use_cdata=True)

        # descrição (limpa)
        safe_add(item, "description", d["description"], use_cdata=True)

        # imagens
        safe_add(item, "image_link", d["image_link"], is_g=True)
        for u in d.get("additional_images", []):
            safe_add(item, "additional_image_link", u, is_g=True)

        # preço/condição/estoque
        safe_add(item, "price", d.get("price", ""), is_g=True)
        if d.get("sale_price"):
            safe_add(item, "sale_price", d["sale_price"], is_g=True)
        safe_add(item, "condition", d.get("condition", "new"), is_g=True)
        safe_add(item, "availability", d.get("availability", "in stock"), is_g=True)

        # ids
        safe_add(item, "id", d["id"], is_g=True)

        # brand / category
        safe_add(item, "brand", d.get("brand", ""), is_g=True)
        safe_add(item, "product_type", d.get("product_type", ""), is_g=True)

        # MPN = SKU/ID (como você pediu)
        safe_add(item, "mpn", d["mpn"], is_g=True)

        # GTIN/EAN (se tiver)
        if d.get("gtin"):
            safe_add(item, "gtin", d["gtin"], is_g=True)

        # online_only (mantém)
        safe_add(item, "online_only", "y", is_g=True)

    return etree.ElementTree(rss)


def parse_base_feed(xml_text: str) -> List[dict]:
    """
    Lê XML base e extrai campos essenciais.
    Aceita tanto <g:...> quanto algumas variações.
    """
    parser = etree.XMLParser(recover=True, huge_tree=True)
    root = etree.fromstring(xml_text.encode("utf-8"), parser=parser)

    items = root.findall(".//item")
    out = []

    for it in items:
        # Alguns feeds usam tags sem g: para title/link/description, e g: para outros
        title = etree_text(it.find("title"))
        link = etree_text(it.find("link"))

        gid = etree_text(it.find(ns("id")))
        if not gid:
            # fallback: usar o title como id (não ideal), mas evita quebrar
            gid = title[:50]

        image_link = etree_text(it.find(ns("image_link")))
        image_link = normalize_image_url(image_link)

        # Campos opcionais
        price = etree_text(it.find(ns("price")))
        sale_price = etree_text(it.find(ns("sale_price")))
        availability = etree_text(it.find(ns("availability")), "in stock")
        condition = etree_text(it.find(ns("condition")), "new")
        brand = etree_text(it.find(ns("brand")))
        product_type = etree_text(it.find(ns("product_type")))

        gtin = etree_text(it.find(ns("gtin")))
        # MPN = SKU/ID
        mpn = gid

        out.append({
            "id": gid,
            "title": title,
            "link": link,
            "image_link": image_link,
            "price": price,
            "sale_price": sale_price,
            "availability": availability,
            "condition": condition,
            "brand": brand,
            "product_type": product_type,
            "gtin": gtin,
            "mpn": mpn,
        })

    return out


def enrich_items(base_items: List[dict]) -> List[dict]:
    """
    Para cada produto:
    - busca descrição real do bloco de descrição do site
    - busca galeria e cria additional_image_link
    - garante imagem principal e adicionais em 1600x1600 quando possível
    """
    enriched = []
    total = len(base_items)

    for idx, d in enumerate(base_items, start=1):
        link = d["link"]
        if not link:
            # sem link não tem como enriquecer
            d["description"] = clean_text(d.get("title", ""))
            d["additional_images"] = []
            enriched.append(d)
            continue

        try:
            desc = extract_best_description(link)
        except Exception as e:
            desc = ""
            print(f"[WARN] Falha ao extrair descrição ({idx}/{total}): {link} -> {e}", file=sys.stderr)

        # Se descrição vier vazia, usa título + brand + categoria (mínimo)
        if not desc:
            base_desc = f"{d.get('title','')}"
            if d.get("brand"):
                base_desc += f" | Marca: {d['brand']}"
            if d.get("product_type"):
                base_desc += f" | Categoria: {d['product_type']}"
            desc = clean_text(base_desc)

        # Acrescenta identificadores no final (sem inventar nada)
        # (isso ajuda o Google a casar produto e é “seguro”)
        tail_parts = []
        if d.get("mpn"):
            tail_parts.append(f"MPN: {d['mpn']}")
        if d.get("gtin"):
            tail_parts.append(f"EAN: {d['gtin']}")
        if tail_parts:
            desc = f"{desc}\n\n" + " | ".join(tail_parts)

        desc = limit_description(desc, MAX_DESCRIPTION_CHARS)

        # Galeria
        try:
            gallery = extract_gallery_images(link)
        except Exception as e:
            gallery = []
            print(f"[WARN] Falha ao extrair galeria ({idx}/{total}): {link} -> {e}", file=sys.stderr)

        # Garante que a principal não repita nas adicionais
        main_img = normalize_image_url(d.get("image_link", ""))
        additional = []
        for u in gallery:
            u = normalize_image_url(u)
            if not u or u == main_img:
                continue
            additional.append(u)

        # Se base feed tiver algumas additional_image_link, também dá para aproveitar
        # (mas como a Loja Integrada costuma mandar poucas, aqui priorizamos galeria)

        d["image_link"] = main_img
        d["additional_images"] = additional[:MAX_ADDITIONAL_IMAGES]
        d["description"] = desc

        enriched.append(d)

        if idx % 10 == 0 or idx == total:
            print(f"[INFO] Processados {idx}/{total}")

    return enriched


def write_xml(tree: etree._ElementTree, path: str):
    xml_bytes = etree.tostring(
        tree.getroot(),
        xml_declaration=True,
        encoding="UTF-8",
        pretty_print=True
    )
    with open(path, "wb") as f:
        f.write(xml_bytes)


def main():
    print("[INFO] Baixando XML base...")
    base_xml = http_get(BASE_FEED_URL)

    print("[INFO] Lendo itens do XML base...")
    base_items = parse_base_feed(base_xml)
    print(f"[INFO] Itens encontrados: {len(base_items)}")

    print("[INFO] Enriquecendo itens (descrição + imagens)...")
    enriched = enrich_items(base_items)

    print("[INFO] Montando XML final...")
    out_tree = build_output_feed(enriched)

    print(f"[INFO] Salvando arquivo: {OUTPUT_FILE}")
    write_xml(out_tree, OUTPUT_FILE)

    print("[OK] Feed gerado com sucesso.")


if __name__ == "__main__":
    main()

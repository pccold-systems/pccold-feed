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

# Limites recomendados p/ Merchant (não é “lei”, é prática):
DESC_MIN = 250
DESC_MAX = 800

MAX_ADDITIONAL_IMAGES = 10  # Google aceita várias; 10 costuma ser ótimo


# =========================
# HELPERS (TEXTO)
# =========================
def strip_html_to_text(raw: str) -> str:
    """Remove HTML e retorna texto puro."""
    if not raw:
        return ""
    raw = html.unescape(raw)

    # Se veio um CSS/HTML “colado” (como no seu caso), mata padrões de CSS comuns
    raw = re.sub(r"\{[^{}]*\}", " ", raw)  # blocos { ... }
    raw = re.sub(r"\.[a-zA-Z0-9_-]+\s*\{", " ", raw)  # .classe {
    raw = re.sub(r"</?style[^>]*>", " ", raw, flags=re.I)

    soup = BeautifulSoup(raw, "html.parser")
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def remove_emojis_and_controls(text: str) -> str:
    """Remove emojis e caracteres de controle."""
    if not text:
        return ""
    # remove chars de controle (Cc) e símbolos comuns de emoji
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Cc":
            continue
        # remove “símbolos” que geralmente incluem emoji/pictogramas
        if cat in ("So",):
            continue
        out.append(ch)
    text = "".join(out)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_text(text: str) -> str:
    """Texto final limpo, sem CSS/HTML, sem emoji, sem lixo repetido."""
    text = strip_html_to_text(text)
    text = remove_emojis_and_controls(text)

    # remove “restos” típicos do seu CSS inline
    text = re.sub(r"\b(descricao-produto|tabela-ficha)\b", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def smart_benefits_from_title(title: str) -> list[str]:
    """Extrai benefícios “inteligentes” baseado em sinais do título."""
    t = (title or "").lower()
    benefits = []

    if "pwm" in t:
        benefits.append("Controle PWM para ajuste preciso de rotação.")
    if "argb" in t or "a-rgb" in t:
        benefits.append("Iluminação ARGB para setups gamer.")
    elif "rgb" in t:
        benefits.append("Iluminação RGB para personalização do setup.")
    if "140" in t and "mm" in t:
        benefits.append("Tamanho 140mm: excelente fluxo de ar com eficiência.")
    if "120" in t and "mm" in t:
        benefits.append("Tamanho 120mm: equilíbrio entre desempenho e compatibilidade.")
    if "p14" in t or "p12" in t:
        benefits.append("Ideal para gabinete e radiador, com foco em desempenho térmico.")
    if "silent" in t:
        benefits.append("Projeto voltado para baixo ruído no uso diário.")
    if "pro" in t:
        benefits.append("Linha de alta performance para entusiastas.")
    if "water" in t or "liquid" in t:
        benefits.append("Refrigeração líquida para cargas altas e estabilidade térmica.")

    # mantém no máximo 3 pra não virar “spam”
    return benefits[:3]


def build_optimized_description(
    title: str,
    brand: str,
    product_type: str,
    mpn: str,
    gtin: str,
    original_desc_text: str
) -> str:
    """
    Gera uma descrição otimizada e curta/forte para Shopping.
    - Usa dados estruturados (brand/type/mpn)
    - Usa um resumo do texto original (limpo)
    - Complementa com benefícios inferidos do título
    """
    title = (title or "").strip()
    brand = (brand or "").strip()
    product_type = (product_type or "").strip()
    mpn = (mpn or "").strip()
    gtin = (gtin or "").strip()

    base = []

    # 1) Primeira linha: identificação
    header_parts = []
    if title:
        header_parts.append(title)
    if brand and brand.lower() not in (title or "").lower():
        header_parts.append(f"Marca {brand}")
    if mpn:
        header_parts.append(f"Modelo/MPN {mpn}")
    if gtin:
        header_parts.append(f"EAN {gtin}")

    if header_parts:
        base.append(" - ".join(header_parts) + ".")

    # 2) Um resumo do texto original (se vier bom)
    cleaned = normalize_text(original_desc_text)

    # Se o texto original tiver muito “ruído”, pega só o começo de forma segura
    if cleaned:
        # corta em um ponto razoável
        snippet = cleaned[:500]
        # tenta cortar no último ponto pra não terminar “picado”
        cut = snippet.rfind(".")
        if cut > 120:
            snippet = snippet[:cut+1]
        base.append(snippet)

    # 3) Benefícios inferidos
    benefits = smart_benefits_from_title(title)
    if benefits:
        base.append(" ".join(benefits))

    # 4) Categoria (ajuda o algoritmo sem virar keyword stuffing)
    if product_type:
        base.append(f"Categoria: {product_type}.")

    # monta e limita tamanho
    final = " ".join([x for x in base if x]).strip()
    final = remove_emojis_and_controls(final)
    final = re.sub(r"\s+", " ", final).strip()

    # Se ficou pequeno demais, reforça com uma frase neutra
    if len(final) < DESC_MIN:
        extra = "Produto ideal para upgrades de refrigeração com foco em performance e confiabilidade."
        final = (final + " " + extra).strip()

    # Limita
    if len(final) > DESC_MAX:
        final = final[:DESC_MAX].rstrip()
        # tenta fechar frase
        last_dot = final.rfind(".")
        if last_dot > 200:
            final = final[:last_dot+1]

    return final


# =========================
# HELPERS (IMAGENS)
# =========================
def force_high_res_cdn(url: str) -> str:
    """
    Loja Integrada/AWSLI costuma ter variantes /300x300/, /800x800/, etc.
    Aqui tentamos forçar 1600x1600 quando aparecer esse padrão no caminho.
    """
    if not url:
        return url
    # troca apenas se tiver um bloco NxN no path
    url2 = re.sub(r"/\d{2,4}x\d{2,4}/", "/1600x1600/", url)
    return url2


def extract_gallery_images_from_product_page(product_url: str) -> list[str]:
    """
    Extrai imagens do HTML do produto (fallback para pegar galeria).
    Observação: pode variar conforme tema. Fizemos heurística ampla.
    """
    if not product_url:
        return []

    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get(product_url, headers=headers, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")

        urls = set()

        # 1) og:image
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            urls.add(og["content"].strip())

        # 2) imagens em carrosséis comuns
        for img in soup.select("img"):
            src = img.get("src") or ""
            data_src = img.get("data-src") or ""
            cand = src.strip() or data_src.strip()
            if not cand:
                continue
            # filtra ícones e thumbnails pequenos típicos
            if "data:image" in cand:
                continue
            if any(x in cand.lower() for x in ["sprite", "logo", "favicon"]):
                continue
            # pega só imagens da cdn awsli (comum na LI)
            if "cdn.awsli.com.br" in cand:
                urls.add(cand)

        # normaliza e ordena
        out = []
        for u in urls:
            out.append(force_high_res_cdn(u))
        return list(dict.fromkeys(out))  # preserva ordem sem duplicar
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

    # Namespace g
    nsmap = root.nsmap.copy()
    gns = nsmap.get("g")
    if not gns:
        # fallback se vier diferente
        gns = "http://base.google.com/ns/1.0"
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

        # ID/SKU
        pid = get_text(item, g + "id") or get_text(item, "g:id")

        # MPN: se não existir, usa SKU/ID
        mpn_el = item.find(g + "mpn")
        if mpn_el is None:
            mpn_el = etree.SubElement(item, g + "mpn")
        mpn_el.text = pid

        # GTIN/EAN: mantém se existir; se vier vazio, tenta achar no XML base
        gtin = get_text(item, g + "gtin")

        # ===== DESCRIÇÃO OTIMIZADA =====
        desc_el = item.find("description")
        original_desc = desc_el.text if desc_el is not None and desc_el.text else ""
        optimized = build_optimized_description(
            title=title,
            brand=brand,
            product_type=product_type,
            mpn=pid,
            gtin=gtin,
            original_desc_text=original_desc
        )
        if desc_el is None:
            desc_el = etree.SubElement(item, "description")
        set_cdata_text(desc_el, optimized)

        # ===== IMAGENS =====
        # imagem principal
        img_el = item.find(g + "image_link")
        if img_el is not None and img_el.text:
            img_el.text = force_high_res_cdn(img_el.text.strip())

        # imagens adicionais:
        # 1) se já existir no base, força high-res e limita quantidade
        additional = item.findall(g + "additional_image_link")
        if additional:
            fixed = []
            for a in additional:
                if a.text:
                    fixed.append(force_high_res_cdn(a.text.strip()))
            fixed = list(dict.fromkeys(fixed))[:MAX_ADDITIONAL_IMAGES]

            # limpa e recria
            for a in additional:
                item.remove(a)
            for u in fixed:
                new_a = etree.SubElement(item, g + "additional_image_link")
                new_a.text = u
        else:
            # 2) fallback: pega galeria direto da página do produto
            gallery = extract_gallery_images_from_product_page(link)
            gallery = [u for u in gallery if u]  # remove vazios

            # remove a principal da lista, se duplicar
            main_img = img_el.text.strip() if img_el is not None and img_el.text else ""
            gallery = [u for u in gallery if u != main_img]

            gallery = gallery[:MAX_ADDITIONAL_IMAGES]
            for u in gallery:
                new_a = etree.SubElement(item, g + "additional_image_link")
                new_a.text = u

        # opcional: garante condition
        cond_el = item.find(g + "condition")
        if cond_el is None:
            cond_el = etree.SubElement(item, g + "condition")
            cond_el.text = "new"

    # escreve feed final
    xml_bytes = etree.tostring(
        root,
        pretty_print=True,
        xml_declaration=True,
        encoding="UTF-8"
    )
    with open(OUTPUT_FILE, "wb") as f:
        f.write(xml_bytes)

    print(f"Feed gerado: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()

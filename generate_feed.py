# -*- coding: utf-8 -*-
"""
PcCold - Google Merchant Feed Generator
- Lê o XML da plataforma (Loja Integrada / awsli)
- Converte description para TEXTO PURO (remove HTML/CSS e caracteres especiais)
- Gera <g:additional_image_link> quando encontrar mais imagens
- Garante gtin (EAN) quando existir
- Mantém sku como mpn quando fizer sentido
- Publica o XML final como google-merchant.xml na raiz do repo
"""

import re
import unicodedata
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from lxml import etree


# =========================
# 1) CONFIG: feed original da loja (SEU LINK JÁ AQUI)
# =========================
SOURCE_FEED_URL = "https://www.pccold.com.br/xml/6301c/googlemerchant.xml"


# =========================
# 2) Utilitários de limpeza
# =========================

def strip_html_to_text(html: str) -> str:
    """Remove HTML e devolve texto."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["style", "script"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return text


def remove_css_like_blocks(text: str) -> str:
    """Remove trechos tipo '.classe { ... }' e outras sobras comuns."""
    if not text:
        return ""

    text = re.sub(r"\.[A-Za-z0-9_-]+\s*\{[^}]*\}", " ", text, flags=re.DOTALL)
    text = re.sub(r"\#[A-Za-z0-9_-]+\s*\{[^}]*\}", " ", text, flags=re.DOTALL)
    text = re.sub(r":contentReference\[[^\]]+\]\{[^\}]+\}", " ", text)

    return text


def remove_emojis_and_specials(text: str) -> str:
    """
    Remove emojis e caracteres especiais não ASCII.
    Mantém letras, números e pontuação básica.
    """
    if not text:
        return ""

    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if 32 <= ord(ch) <= 126)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_description(raw: str) -> str:
    """Converte para texto puro e remove HTML/CSS/emojis/caracteres especiais."""
    text = strip_html_to_text(raw)
    text = remove_css_like_blocks(text)
    text = remove_emojis_and_specials(text)
    return text


# =========================
# 3) Leitura e geração do feed
# =========================

def fetch_xml(url: str) -> bytes:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content


def parse_xml(content: bytes) -> etree._Element:
    parser = etree.XMLParser(recover=True, remove_blank_text=True)
    return etree.fromstring(content, parser=parser)


def get_text(node):
    if node is None:
        return ""
    return (node.text or "").strip()


def set_text(node, value: str):
    node.text = value


def findall_items(root):
    channel = root.find("channel")
    if channel is None:
        return []
    return channel.findall("item")


def find_tag(item, tagname, ns=None):
    if tagname.startswith("g:"):
        if ns is None:
            ns = {"g": "http://base.google.com/ns/1.0"}
        return item.find(tagname, namespaces=ns)
    return item.find(tagname)


def findall_tag(item, tagname, ns=None):
    if tagname.startswith("g:"):
        if ns is None:
            ns = {"g": "http://base.google.com/ns/1.0"}
        return item.findall(tagname, namespaces=ns)
    return item.findall(tagname)


def add_g_tag(item, tag, value, ns_uri="http://base.google.com/ns/1.0"):
    node = etree.SubElement(item, f"{{{ns_uri}}}{tag}")
    node.text = value
    return node


def build_additional_images(item, ns):
    """
    Só dá pra exportar <g:additional_image_link> se o feed original trouxer
    essas URLs em algum lugar (múltiplos image_link ou algum campo com lista).
    """
    gns_uri = "http://base.google.com/ns/1.0"

    # 1) Se houver mais de um g:image_link
    image_links = findall_tag(item, "g:image_link", ns)
    if len(image_links) > 1:
        main = get_text(image_links[0])
        for extra_node in image_links[1:]:
            extra_url = get_text(extra_node)
            item.remove(extra_node)
            if extra_url and extra_url != main:
                add_g_tag(item, "additional_image_link", extra_url, gns_uri)
        return

    # 2) Campos alternativos (se existirem no seu feed)
    possible_fields = ["additional_images", "image_links", "images", "galeria", "gallery"]
    for field in possible_fields:
        node = item.find(field)
        if node is not None:
            raw = get_text(node)
            if raw:
                parts = re.split(r"[,\|\n]+", raw)
                parts = [p.strip() for p in parts if p.strip()]
                main = get_text(find_tag(item, "g:image_link", ns))
                for u in parts:
                    if u and u != main:
                        add_g_tag(item, "additional_image_link", u, gns_uri)
            item.remove(node)
            return


def ensure_gtin_mpn(item, ns):
    """
    - Se não existir g:mpn, cria g:mpn = g:id (SKU)
    - Se gtin estiver vazio, tenta achar um número (8/12/13/14 dígitos) nos campos
    """
    gns_uri = "http://base.google.com/ns/1.0"

    gid = get_text(find_tag(item, "g:id", ns))
    mpn_node = find_tag(item, "g:mpn", ns)
    gtin_node = find_tag(item, "g:gtin", ns)

    # MPN = SKU
    if mpn_node is None and gid:
        add_g_tag(item, "mpn", gid, gns_uri)

    # GTIN node
    if gtin_node is None:
        gtin_node = add_g_tag(item, "gtin", "", gns_uri)

    if get_text(gtin_node):
        return

    # tenta em campos comuns
    candidates = []
    for t in ["g:gtin", "g:barcode", "g:ean", "ean", "barcode", "gtin"]:
        n = find_tag(item, t, ns) if ":" in t else item.find(t)
        if n is not None:
            candidates.append(get_text(n))

    # tenta título/descrição
    title = get_text(find_tag(item, "title", ns))
    desc = get_text(find_tag(item, "description", ns))
    candidates.extend([title, desc])

    ean = ""
    for c in candidates:
        if not c:
            continue
        m = re.search(r"\b(\d{8}|\d{12}|\d{13}|\d{14})\b", c)
        if m:
            ean = m.group(1)
            break

    if ean:
        set_text(gtin_node, ean)


def main():
    xml_bytes = fetch_xml(SOURCE_FEED_URL)
    root = parse_xml(xml_bytes)

    gns = {"g": "http://base.google.com/ns/1.0"}

    for item in findall_items(root):
        # description -> texto puro
        desc_node = find_tag(item, "description", gns)
        if desc_node is not None:
            desc_node.text = clean_description(desc_node.text or "")

        # additional images (se o feed original permitir)
        build_additional_images(item, gns)

        # gtin (EAN) e mpn (SKU)
        ensure_gtin_mpn(item, gns)

    # channel: lastBuildDate
    channel = root.find("channel")
    if channel is not None:
        gen = channel.find("lastBuildDate")
        if gen is None:
            gen = etree.SubElement(channel, "lastBuildDate")
        gen.text = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

    out = etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)

    with open("google-merchant.xml", "wb") as f:
        f.write(out)

    print("OK: google-merchant.xml gerado com sucesso.")


if __name__ == "__main__":
    main()

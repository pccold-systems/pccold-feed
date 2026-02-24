# generate_feed.py
# Gera um novo feed Google Merchant (google-merchant.xml) a partir do XML da Loja Integrada,
# enriquecendo com:
# - descrição otimizada (a partir do TEXTO do seu site, sem CSS)
# - múltiplas imagens (galeria), priorizando 1600x1600 quando possível
# - GTIN (EAN) e MPN (SKU) quando existirem
#
# Requisitos: requests, beautifulsoup4, lxml  (já estão no requirements.txt)

from __future__ import annotations

import re
import sys
import html
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from lxml import etree


BASE_FEED_URL = "https://www.pccold.com.br/xml/6301c/googlemerchant.xml"
OUTPUT_FILE = "google-merchant.xml"
SITE_DOMAIN = "www.pccold.com.br"

UA = "PcColdFeedBot/1.0 (+https://pccold-systems.github.io/pccold-feed/)"

# Evitar imagens “lixo” comuns no AWSLI / Loja Integrada
BAD_IMAGE_SUBSTRINGS = [
    "produto-sem-imagem",
    "stamp_google_safe_browsing",
    "/static/img/",
    "data:image",
]

# Merchant aceita várias additional_image_link, mas não precisa exagerar.
MAX_ADDITIONAL_IMAGES = 10


def http_get(url: str, timeout: int = 25) -> requests.Response:
    headers = {"User-Agent": UA, "Accept": "text/html,application/xml;q=0.9,*/*;q=0.8"}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r


def normalize_image_url(url: str) -> Optional[str]:
    if not url:
        return None

    u = url.strip().replace(" ", "%20")
    # Normaliza scheme-relative //cdn...
    if u.startswith("//"):
        u = "https:" + u

    low = u.lower()
    for bad in BAD_IMAGE_SUBSTRINGS:
        if bad in low:
            return None

    # Força 1600x1600 quando o CDN usa tamanhos no path
    # Exemplos vistos: /800x800/, /64x50/, /300x300/, /600x1000/
    u = re.sub(r"/\d{2,4}x\d{2,4}/", "/1600x1600/", u)

    # Alguns links podem vir como http:
    u = u.replace("http://", "https://")

    # Sanidade: só aceita URLs
    try:
        p = urlparse(u)
        if not p.scheme or not p.netloc:
            return None
    except Exception:
        return None

    return u


def uniq_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def extract_product_description_text(soup: BeautifulSoup) -> str:
    """
    Extrai texto visível do bloco principal de descrição do produto.
    Estratégia:
      - remover script/style/noscript
      - achar um container que contenha "Principais Vantagens" (se existir)
      - caso não exista, usar o maior bloco de texto dentro de main/article/section
      - parar antes de “Produtos relacionados” (quando aparecer)
    Retorna texto limpo (sem CSS).
    """
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    full_text = soup.get_text("\n", strip=True)

    # Corte “Produtos relacionados” pra baixo, porque ali vira lista de outros produtos
    cut_markers = ["Produtos relacionados", "Produtos Relacionados"]
    for m in cut_markers:
        idx = full_text.find(m)
        if idx != -1:
            full_text = full_text[:idx].strip()
            break

    # Se tiver “Principais Vantagens”, tenta pegar um container mais “cirúrgico”
    anchor = soup.find(string=re.compile(r"Principais Vantagens", re.I))
    if anchor:
        cand = anchor
        # sobe alguns níveis tentando achar um bloco que também tenha “Inclui na Embalagem”
        best = None
        for _ in range(8):
            cand = cand.parent if cand else None
            if not cand or not hasattr(cand, "get_text"):
                break
            t = cand.get_text("\n", strip=True)
            if "Inclui na Embalagem" in t or "Ideal Para" in t or "Especificações" in t:
                best = t
                # continua subindo um pouco, mas guarda o último bom
        if best and len(best) >= 200:
            txt = best
        else:
            txt = full_text
    else:
        txt = full_text

    # Limpeza: remover linhas muito curtas/repetitivas
    lines = [ln.strip() for ln in txt.splitlines()]
    lines = [ln for ln in lines if ln and len(ln) >= 2]

    # Remove breadcrumb repetido e coisas de navegação comuns
    junk_patterns = [
        r"^Início$",
        r"^Lista de Desejos$",
        r"^Calcule o frete$",
        r"^OK$",
        r"^Não sei meu CEP.*$",
        r"^Este prazo de entrega.*$",
        r"^Qtde:.*$",
        r"^Parcelas$",
        r"^\d+x de R\$.*$",
        r"^R\$.*via Pix$",
    ]
    cleaned = []
    for ln in lines:
        if any(re.match(p, ln) for p in junk_patterns):
            continue
        cleaned.append(ln)

    # Compacta
    txt2 = " ".join(cleaned)
    txt2 = re.sub(r"\s+", " ", txt2).strip()

    return txt2


def pick_key_specs(text: str) -> Dict[str, str]:
    """
    Pega alguns números importantes do texto do produto, quando existirem,
    sem inventar nada.
    """
    specs = {}

    def grab(pattern: str, key: str):
        m = re.search(pattern, text, re.I)
        if m:
            specs[key] = m.group(1).strip()

    grab(r"Velocidade\s+(\d[\d\.\,]*\s*RPM)", "rpm")
    grab(r"Fluxo de ar\s+(\d[\d\.\,]*\s*CFM)", "cfm")
    grab(r"N[ií]vel de ru[ií]do\s+(\d[\d\.\,]*\s*dBA)", "dba")
    grab(r"Press[aã]o est[aá]tica\s+(\d[\d\.\,]*\s*mm\s*H[₂2]O)", "sp")
    grab(r"Dimens[õo]es\s+(\d+\s*[×x]\s*\d+\s*[×x]\s*\d+\s*mm)", "size")

    # Conector: “3-Pin (DC)”, “4-Pin (PWM)”
    m = re.search(r"Conector\s+([^\.\|]+?)(?:Velocidade|Fluxo|Press[aã]o|N[ií]vel|Corrente|Tens[aã]o|Rolamento|$)", text, re.I)
    if m:
        c = m.group(1).strip()
        c = re.sub(r"\s+", " ", c)
        specs["conn"] = c

    return specs


def build_merchant_description(
    title: str,
    brand: str,
    mpn: str,
    gtin: str,
    base_text: str,
) -> str:
    """
    Monta uma descrição “profissional” e única:
    - usa o texto real da página (base_text)
    - extrai specs (se existirem)
    - cria 2–4 frases, sem CSS, sem tags
    """
    base_text = base_text.strip()

    # Pega o 1º parágrafo “de verdade” como gancho
    # (até o primeiro “Principais Vantagens”, se existir)
    hook = base_text
    cut = re.search(r"\bPrincipais Vantagens\b", base_text, re.I)
    if cut:
        hook = base_text[: cut.start()].strip()

    # Se hook ficou gigante, encurta
    hook = re.sub(r"\s+", " ", hook).strip()
    if len(hook) > 320:
        hook = hook[:320].rsplit(" ", 1)[0] + "…"

    specs = pick_key_specs(base_text)

    bits = []
    if hook:
        bits.append(hook)

    # Linha de specs (só o que tiver)
    spec_parts = []
    if "rpm" in specs:
        spec_parts.append(f"{specs['rpm']}")
    if "cfm" in specs:
        spec_parts.append(f"{specs['cfm']}")
    if "sp" in specs:
        spec_parts.append(f"{specs['sp']}")
    if "dba" in specs:
        spec_parts.append(f"{specs['dba']}")
    if "conn" in specs:
        spec_parts.append(f"Conector {specs['conn']}")
    if "size" in specs:
        spec_parts.append(f"Dimensões {specs['size']}")

    if spec_parts:
        bits.append("Especificações-chave: " + " | ".join(spec_parts) + ".")

    # Identificação do produto (ajuda unicidade, e o Merchant gosta de consistência)
    id_line = []
    if brand:
        id_line.append(brand)
    if mpn:
        id_line.append(f"MPN {mpn}")
    if gtin:
        id_line.append(f"EAN {gtin}")
    if id_line:
        bits.append(" • ".join(id_line) + ".")

    # Junta
    desc = " ".join(bits)
    desc = re.sub(r"\s+", " ", desc).strip()

    # Limite seguro (Merchant permite bem mais, mas isso mantém enxuto e forte)
    if len(desc) > 950:
        desc = desc[:950].rsplit(" ", 1)[0] + "…"

    return desc


def extract_gallery_images(soup: BeautifulSoup) -> List[str]:
    """
    Coleta imagens da galeria do produto (img src / data-src / etc),
    normaliza pra 1600x1600, remove placeholders, deduplica.
    """
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    candidates = []

    # img tags
    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-original", "data-lazy", "data-zoom-image"):
            u = img.get(attr)
            nu = normalize_image_url(u) if u else None
            if nu:
                candidates.append(nu)

    # links que apontam direto pra imagem
    for a in soup.find_all("a"):
        href = a.get("href")
        if href and ("cdn.awsli.com.br" in href or href.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))):
            nu = normalize_image_url(href)
            if nu:
                candidates.append(nu)

    candidates = uniq_keep_order(candidates)

    # Filtro final: manter só AWSLI/CDN e imagens
    out = []
    for u in candidates:
        low = u.lower()
        if "cdn.awsli.com.br" not in low:
            continue
        if not re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", low):
            continue
        out.append(u)

    return uniq_keep_order(out)


def parse_base_feed(xml_bytes: bytes) -> etree._Element:
    parser = etree.XMLParser(recover=True, encoding="utf-8")
    return etree.fromstring(xml_bytes, parser=parser)


def get_text(el: Optional[etree._Element]) -> str:
    return (el.text or "").strip() if el is not None else ""


def set_text(el: etree._Element, value: str):
    el.text = value


def ensure_child(parent: etree._Element, tag: str) -> etree._Element:
    ch = parent.find(tag)
    if ch is None:
        ch = etree.SubElement(parent, tag)
    return ch


def main():
    try:
        base_xml = http_get(BASE_FEED_URL).content
    except Exception as e:
        print(f"[ERRO] Não consegui baixar o XML base: {e}", file=sys.stderr)
        sys.exit(1)

    root = parse_base_feed(base_xml)

    # Namespaces
    nsmap = root.nsmap.copy()
    # Garante prefixo g
    if "g" not in nsmap or not nsmap["g"]:
        nsmap["g"] = "http://base.google.com/ns/1.0"

    gns = "{http://base.google.com/ns/1.0}"

    channel = root.find("channel")
    if channel is None:
        print("[ERRO] XML base sem <channel>.", file=sys.stderr)
        sys.exit(1)

    items = channel.findall("item")
    print(f"[OK] Itens no XML base: {len(items)}")

    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    for idx, item in enumerate(items, start=1):
        title = get_text(item.find("title"))
        link = get_text(item.find("link"))
        brand = get_text(item.find(f"{gns}brand"))
        mpn = get_text(item.find(f"{gns}mpn"))
        gtin = get_text(item.find(f"{gns}gtin"))
        pid = get_text(item.find(f"{gns}id"))

        # Sanidade: só tenta enriquecer links do seu domínio
        if not link or SITE_DOMAIN not in link:
            continue

        try:
            r = session.get(link, timeout=25)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            print(f"[WARN] Falha ao abrir página ({idx}/{len(items)}): {link} -> {e}")
            continue

        # 1) Descrição otimizada baseada NO TEXTO DO SITE
        base_text = extract_product_description_text(soup)
        if base_text:
            new_desc = build_merchant_description(
                title=title,
                brand=brand,
                mpn=mpn or pid,   # se faltar, cai pro id
                gtin=gtin,
                base_text=base_text,
            )
            desc_el = ensure_child(item, "description")
            # Usa CDATA via string “normal” (lxml ao serializar preserva caracteres)
            set_text(desc_el, new_desc)

        # 2) Imagens (principal + adicionais)
        gallery = extract_gallery_images(soup)

        # Se a imagem principal do XML base existir, tenta normalizar também
        base_main_img = get_text(item.find(f"{gns}image_link"))
        base_main_img = normalize_image_url(base_main_img) if base_main_img else None

        # Ordena: prioriza a do XML base (se válida), senão a primeira da galeria
        merged = []
        if base_main_img:
            merged.append(base_main_img)
        merged.extend(gallery)
        merged = uniq_keep_order(merged)

        if merged:
            # define image_link (1ª)
            img_el = ensure_child(item, f"{gns}image_link")
            set_text(img_el, merged[0])

            # remove additional antigos
            for old in item.findall(f"{gns}additional_image_link"):
                item.remove(old)

            # adiciona adicionais (até limite)
            additional = merged[1 : 1 + MAX_ADDITIONAL_IMAGES]
            for u in additional:
                ael = etree.SubElement(item, f"{gns}additional_image_link")
                ael.text = u

        # 3) MPN = SKU (se você quer sempre SKU como MPN)
        # Se não existir mpn, cria com o g:id (ou mantém o que já tiver)
        if not mpn:
            mpn_el = ensure_child(item, f"{gns}mpn")
            set_text(mpn_el, pid)

        # 4) GTIN/EAN: mantém se já existe; se vazio, não inventa.
        # (Você pode completar depois no cadastro, mas aqui não “chutamos”)
        # Só garante que não fique com espaços estranhos
        if gtin:
            gtin_el = ensure_child(item, f"{gns}gtin")
            set_text(gtin_el, re.sub(r"\D", "", gtin))

        if idx % 25 == 0:
            print(f"[OK] Processados {idx}/{len(items)}...")

    # Serializa bonito
    xml_out = etree.tostring(
        root, pretty_print=True, xml_declaration=True, encoding="UTF-8"
    )

    with open(OUTPUT_FILE, "wb") as f:
        f.write(xml_out)

    print(f"[OK] Feed gerado: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Novimz Auto-Publisher — versão Python/GitHub Actions
=======================================================

Replica, sem custo recorrente, o pipeline que corria no Make.com:

RSS -> [filtro editorial + deduplicação] -> Google Gemini AI (texto)
-> Cloudflare Workers AI / FLUX.1-schnell (imagem)
-> WordPress (upload de media + criação do post)

Todas as credenciais vêm de variáveis de ambiente (GitHub Actions secrets).
Este script nunca lê nem escreve segredos em disco. Ver README.md para a
lista completa de secrets necessários e instruções de configuração.

Uso:
  python main.py              # corre todos os sites/fontes do config.yaml
  python main.py --dry-run    # faz tudo menos publicar no WordPress
"""

from __future__ import annotations

import argparse
import base64
import difflib
import html
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import requests
import yaml

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "published.json")

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4/accounts"

HTTP_TIMEOUT = 60

# Alguns hosts WordPress (com Mod_Security/WAF) bloqueiam pedidos cujo
# User-Agent seja o valor por omissão da biblioteca "requests" (identifica-se
# como bot/scanner e é rejeitado com 406 Not Acceptable). Usar um User-Agent
# de browser real evita esse bloqueio.
WP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

REQUIRED_JSON_KEYS = [
    "titulo_post",
    "artigo_html",
    "meta_description",
    "alt_text",
    "slug",
    "categoria",
]

# ---------------------------------------------------------------------------
# Utilitários gerais
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"publicados": []}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("publicados", [])
        return data
    except (json.JSONDecodeError, OSError):
        log("AVISO: state/published.json ilegível ou corrompido — a começar com histórico vazio.")
        return {"publicados": []}

def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def prune_state(state: dict, janela_dias: int) -> dict:
    limite = datetime.now(timezone.utc) - timedelta(days=janela_dias * 3)
    mantidos = []
    for item in state.get("publicados", []):
        try:
            ts = datetime.fromisoformat(item["publicado_em"])
        except (KeyError, ValueError):
            continue
        if ts >= limite:
            mantidos.append(item)
    state["publicados"] = mantidos
    return state

def slugify(texto: str) -> str:
    """Rede de segurança caso o slug devolvido pela Gemini não esteja limpo."""
    nfkd = unicodedata.normalize("NFKD", texto)
    sem_acentos = "".join(c for c in nfkd if not unicodedata.combining(c))
    sem_acentos = sem_acentos.lower()
    sem_acentos = re.sub(r"[^a-z0-9]+", "-", sem_acentos)
    sem_acentos = re.sub(r"-+", "-", sem_acentos).strip("-")
    return sem_acentos[:70].strip("-")

def titulos_semelhantes(a: str, b: str, limiar: float) -> bool:
    ratio = difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()
    return ratio >= limiar

def env_required(nome_var: str) -> str:
    valor = os.environ.get(nome_var)
    if not valor:
        raise RuntimeError(
            f"Variável de ambiente obrigatória '{nome_var}' não definida. "
            f"Configura-a como secret no GitHub Actions (ver README.md)."
        )
    return valor

# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------

def _rss_text(elem) -> str:
    if elem is None:
        return ""
    # Usa itertext() em vez de elem.text: cobre tanto o caso normal (HTML
    # escapado dentro do texto, o mais comum em RSS bem formado) como feeds
    # menos ortodoxos que colocam tags XML literais/CDATA dentro do campo
    # (elem.text sozinho só devolveria o texto antes da primeira sub-tag).
    texto = "".join(elem.itertext())
    return html.unescape(texto.strip())

def fetch_rss_items(url: str) -> list[dict]:
    """Faz parsing simples de um feed RSS 2.0 (suficiente para as fontes
    usadas: WordPress /feed/, BBC, Google News). Evita depender de uma lib
    externa de parsing de feeds para manter as dependências mínimas."""
    resp = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": "NovimzAutoPublisher/1.0"})
    resp.raise_for_status()
    root = ElementTree.fromstring(resp.content)

    itens = []
    for item in root.iter("item"):
        titulo = _rss_text(item.find("title"))
        descricao = _rss_text(item.find("description"))
        pub_date_raw = _rss_text(item.find("pubDate"))
        source_elem = item.find("source")
        nome_fonte = _rss_text(source_elem) if source_elem is not None else ""

        pub_date = None
        if pub_date_raw:
            try:
                pub_date = parsedate_to_datetime(pub_date_raw)
                if pub_date.tzinfo is None:
                    pub_date = pub_date.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pub_date = None

        # Descrição de feeds do Google News costuma vir com HTML/links — retira tags simples.
        descricao_limpa = re.sub(r"<[^>]+>", " ", descricao)
        descricao_limpa = re.sub(r"\s+", " ", descricao_limpa).strip()

        itens.append(
            {
                "titulo": titulo,
                "descricao": descricao_limpa,
                "pub_date": pub_date,
                "nome_fonte": nome_fonte,
            }
        )
    return itens

def filtrar_itens_recentes(itens: list[dict], lookback_hours: float, max_itens: int) -> list[dict]:
    limite = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    validos = []
    for item in itens:
        if not item["titulo"]:
            continue
        if item["pub_date"] is not None and item["pub_date"] < limite:
            continue
        validos.append(item)
    return validos[:max_itens]

def intercalar_por_fonte(listas: list[list[dict]]) -> list[dict]:
    """Intercala (round-robin) os itens de várias fontes, em vez de esgotar
    uma fonte antes de passar à seguinte — dá a todas as fontes uma hipótese
    igual de contribuir para o alvo de publicações por categoria."""
    resultado = []
    max_len = max((len(lst) for lst in listas), default=0)
    for i in range(max_len):
        for lst in listas:
            if i < len(lst):
                resultado.append(lst[i])
    return resultado

# ---------------------------------------------------------------------------
# Google Gemini AI (texto) — modo RSS (notícia de origem fixa)
# ---------------------------------------------------------------------------

def gerar_artigo_gemini(cfg: dict, api_key: str, titulo: str, descricao: str, nome_fonte: str) -> dict:
    modelo = cfg["definicoes_globais"]["gemini_model"]
    url = f"{GEMINI_API_BASE}/{modelo}:generateContent?key={api_key}"

    user_message = cfg["gemini_user_message_template"].format(
        titulo=titulo or "(sem título)",
        descricao=descricao or "(sem descrição)",
        fonte_nome=nome_fonte or "(fonte desconhecida)",
    )

    payload = {
        "system_instruction": {"parts": [{"text": cfg["gemini_system_prompt"]}]},
        "contents": [{"role": "user", "parts": [{"text": user_message}]}],
        "generationConfig": {
            "temperature": cfg["definicoes_globais"]["gemini_temperature"],
            "topP": cfg["definicoes_globais"]["gemini_top_p"],
            "responseMimeType": "application/json",
        },
    }

    resp = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
    if not resp.ok:
        raise RuntimeError(f"Gemini API devolveu {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    try:
        texto_json = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Resposta da Gemini em formato inesperado: {json.dumps(data)[:500]}") from exc

    artigo = json.loads(texto_json)

    faltam = [k for k in REQUIRED_JSON_KEYS if k not in artigo or not str(artigo[k]).strip()]
    if faltam:
        raise RuntimeError(f"JSON da Gemini incompleto, faltam/estão vazias as chaves: {faltam}")

    return artigo

# ---------------------------------------------------------------------------
# Google Gemini AI (texto) — modo PESQUISA (Google Search grounding, sem RSS)
# ---------------------------------------------------------------------------

def _extrair_json_tolerante(texto: str) -> dict:
    """Extrai um objecto JSON de uma resposta de texto livre da Gemini.

    Usado apenas no modo de pesquisa (`gerar_artigo_gemini_pesquisa`): quando
    a ferramenta `google_search` está activa não é seguro pedir também
    `responseMimeType: application/json` (as duas funcionalidades nem sempre
    são combináveis de forma fiável na API da Gemini) — por isso aqui
    confiamos na instrução do prompt para devolver só JSON, mas ainda assim
    toleramos blocos de código markdown (```json ... ```) ou texto residual
    antes/depois do objecto, em vez de rebentar com json.JSONDecodeError."""
    texto = texto.strip()
    texto = re.sub(r"^```(?:json)?\s*", "", texto)
    texto = re.sub(r"```\s*$", "", texto)
    texto = texto.strip()
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        pass
    inicio = texto.find("{")
    fim = texto.rfind("}")
    if inicio == -1 or fim == -1 or fim <= inicio:
        raise RuntimeError(f"Não foi possível extrair JSON da resposta da Gemini: {texto[:500]}")
    return json.loads(texto[inicio : fim + 1])

def gerar_artigo_gemini_pesquisa(
    cfg: dict, api_key: str, tema: dict, titulos_recentes: list[str]
) -> tuple[dict | None, str]:
    """Gera um artigo original SEM notícia de origem em RSS: pede à própria
    Gemini para pesquisar no Google (ferramenta `google_search`, grounding)
    uma notícia REAL e recente sobre `tema` e escrever o artigo com base
    nela — nunca inventando factos sem uma fonte real encontrada.

    Devolve (artigo_dict, url_da_fonte) em caso de sucesso, ou (None, motivo)
    se a Gemini não devolver nenhuma fonte de pesquisa real (groundingMetadata
    vazio) — nesse caso o chamador NÃO deve publicar nada, precisamente para
    impedir que "notícias" inventadas cheguem ao WordPress."""
    modelo = cfg["definicoes_globais"]["gemini_model"]
    url = f"{GEMINI_API_BASE}/{modelo}:generateContent?key={api_key}"

    lista_recentes = "\n".join(f"- {t}" for t in titulos_recentes[-25:]) or "(nenhum artigo recente ainda)"
    user_message = cfg["gemini_pesquisa_user_message_template"].format(
        instrucao_pesquisa=tema["instrucao_pesquisa"],
        categoria=tema["categoria"],
        titulos_recentes=lista_recentes,
    )

    payload = {
        "system_instruction": {"parts": [{"text": cfg["gemini_pesquisa_system_prompt"]}]},
        "contents": [{"role": "user", "parts": [{"text": user_message}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {
            "temperature": cfg["definicoes_globais"]["gemini_temperature"],
            "topP": cfg["definicoes_globais"]["gemini_top_p"],
        },
    }

    resp = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
    if not resp.ok:
        raise RuntimeError(f"Gemini API (pesquisa) devolveu {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    try:
        candidato = data["candidates"][0]
        texto_resposta = candidato["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Resposta da Gemini (pesquisa) em formato inesperado: {json.dumps(data)[:500]}") from exc

    artigo = _extrair_json_tolerante(texto_resposta)

    faltam = [k for k in REQUIRED_JSON_KEYS if k not in artigo or not str(artigo[k]).strip()]
    if faltam:
        raise RuntimeError(f"JSON da Gemini (pesquisa) incompleto, faltam/estão vazias as chaves: {faltam}")

    # groundingMetadata só existe quando a Gemini efectivamente usou a
    # ferramenta google_search e encontrou resultados — é a nossa garantia
    # de que o artigo está mesmo ancorado numa pesquisa real, e não
    # inventado. Sem isto, recusamos publicar.
    grounding = candidato.get("groundingMetadata") or {}
    chunks = grounding.get("groundingChunks") or []
    fontes_urls = [c["web"]["uri"] for c in chunks if c.get("web", {}).get("uri")]
    if not fontes_urls:
        return None, "sem fontes de pesquisa reais devolvidas pela Gemini (groundingMetadata vazio)"

    return artigo, fontes_urls[0]

# ---------------------------------------------------------------------------
# Cloudflare Workers AI (imagem)
# ---------------------------------------------------------------------------

def gerar_imagem_cloudflare(cfg: dict, account_id: str, api_token: str, alt_text: str) -> bytes:
    modelo = cfg["definicoes_globais"]["cloudflare_model"]
    url = f"{CLOUDFLARE_API_BASE}/{account_id}/ai/run/{modelo}"

    prompt = cfg["cloudflare_image_prompt_template"].format(alt_text=alt_text)
    payload = {
        "prompt": prompt,
        "steps": cfg["definicoes_globais"]["cloudflare_image_steps"],
    }
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    resp = requests.post(url, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
    if not resp.ok:
        raise RuntimeError(f"Cloudflare Workers AI devolveu {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Cloudflare Workers AI reportou falha: {json.dumps(data)[:500]}")

    imagem_b64 = data["result"]["image"]
    return base64.b64decode(imagem_b64)

# ---------------------------------------------------------------------------
# Verificação de qualidade da imagem (Gemini, multimodal)
# ---------------------------------------------------------------------------

IMAGEM_QA_PROMPT = (
    "Estás a validar uma fotografia editorial gerada por IA para um artigo de "
    "noticias, antes de ela ser publicada. Analisa cuidadosamente a imagem e "
    "responde APENAS com um objecto JSON, sem markdown, sem crases, com "
    "exactamente estas duas chaves: \"aprovado\" (true ou false) e \"motivo\" "
    "(uma frase curta em portugues a explicar a decisao).\n\n"
    "Reprova a imagem (aprovado=false) se detectares qualquer um destes "
    "defeitos tipicos de geracao por IA: uma pessoa sem cabeca, ou com a "
    "cabeca em falta/cortada/fundida de forma antinatural com o corpo ou o "
    "fundo; rostos distorcidos, derretidos ou assimetricos de forma bizarra; "
    "maos ou membros deformados, a mais ou em falta; texto ou letras "
    "ilegiveis/sem sentido em letreiros, banners, roupa ou logotipos dentro "
    "da imagem; ou qualquer outro artefacto visualmente quebrado que pareca "
    "pouco profissional para uma fotografia de noticias. Se a imagem parecer "
    "uma fotografia editorial normal, coerente e sem estes defeitos, aprova-a "
    "mesmo que nao seja perfeita esteticamente."
)

def verificar_imagem_gemini(cfg: dict, api_key: str, imagem_bytes: bytes) -> dict:
    """Usa a Gemini (multimodal) para detectar defeitos típicos de imagens
    geradas por IA (ex. figuras humanas sem cabeça, membros deformados, texto
    ilegível em letreiros) antes de a imagem ser publicada. Devolve
    {"aprovado": bool, "motivo": str}.

    IMPORTANTE (corrigido 2026-09-07): em caso de erro na própria verificação
    (ex. API indisponível/sobrecarregada — 429, 503, timeout, etc.), a imagem
    é REPROVADA, não aprovada por omissão. A versão anterior aprovava por
    omissão nestes casos, o que na prática desligava silenciosamente a
    verificação sempre que a quota/disponibilidade da Gemini estava
    apertada — confirmado a acontecer duas vezes em publicações reais no
    mesmo dia (ver `claude/2026-09-06-verificacao-imagem-e-correcao-post.md`
    e o incidente do artigo "Apresentadora de TV no Egito..."), deixando
    passar imagens com defeitos (figura sem cabeça, texto ilegível) sem
    nenhuma verificação real. Tratar uma falha da verificação como reprovação
    faz `gerar_imagem_validada` tentar gerar de novo (até `max_tentativas_imagem`
    vezes) e, se a indisponibilidade persistir em todas as tentativas, o
    artigo simplesmente não é publicado nessa execução — mais fiel ao pedido
    original do Abílio ("antes de publicar, faça análise") do que arriscar
    publicar uma imagem nunca verificada."""
    modelo = cfg["definicoes_globais"]["gemini_model"]
    url = f"{GEMINI_API_BASE}/{modelo}:generateContent?key={api_key}"

    imagem_b64 = base64.b64encode(imagem_bytes).decode("ascii")
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": IMAGEM_QA_PROMPT},
                    {"inline_data": {"mime_type": "image/jpeg", "data": imagem_b64}},
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }

    try:
        resp = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
        if not resp.ok:
            log(f"AVISO: verificação da imagem pela Gemini devolveu {resp.status_code} — a REPROVAR por segurança (sem verificação real, não publica sem confirmar).")
            return {"aprovado": False, "motivo": f"verificação indisponível (HTTP {resp.status_code})"}
        data = resp.json()
        texto_json = data["candidates"][0]["content"]["parts"][0]["text"]
        resultado = json.loads(texto_json)
        return {
            "aprovado": bool(resultado.get("aprovado", True)),
            "motivo": str(resultado.get("motivo", "")),
        }
    except Exception as exc:  # noqa: BLE001
        log(f"AVISO: erro ao verificar imagem pela Gemini ({exc}) — a REPROVAR por segurança (sem verificação real, não publica sem confirmar).")
        return {"aprovado": False, "motivo": f"verificação indisponível ({exc})"}

def gerar_imagem_validada(
    cfg: dict, cf_account_id: str, cf_token: str, gemini_key: str, alt_text: str
) -> tuple[bytes | None, str]:
    """Gera a imagem via Cloudflare e, se `verificar_imagem_com_gemini` estiver
    activo no config.yaml, valida-a pela Gemini — tentando gerar de novo (até
    `max_tentativas_imagem` vezes) sempre que for reprovada. Devolve
    (bytes_da_imagem, "") em caso de sucesso, ou (None, motivo_da_reprovacao)
    se todas as tentativas falharem na verificação."""
    definicoes = cfg["definicoes_globais"]
    verificar = definicoes.get("verificar_imagem_com_gemini", True)
    max_tentativas = definicoes.get("max_tentativas_imagem", 3)

    ultimo_motivo = ""
    for tentativa in range(1, max_tentativas + 1):
        imagem_bytes = gerar_imagem_cloudflare(cfg, cf_account_id, cf_token, alt_text)
        if not verificar:
            return imagem_bytes, ""

        resultado = verificar_imagem_gemini(cfg, gemini_key, imagem_bytes)
        if resultado["aprovado"]:
            if tentativa > 1:
                log(f"Imagem aprovada na tentativa {tentativa}/{max_tentativas}.")
            return imagem_bytes, ""

        ultimo_motivo = resultado["motivo"]
        log(
            f"Imagem reprovada na verificação (tentativa {tentativa}/{max_tentativas}): "
            f"{ultimo_motivo!r} — a tentar gerar de novo."
        )

    return None, ultimo_motivo or "imagem reprovada repetidamente na verificação de qualidade"

# ---------------------------------------------------------------------------
# WordPress REST API
# ---------------------------------------------------------------------------

def publicar_no_wordpress(
    base_url: str,
    wp_user: str,
    wp_app_password: str,
    imagem_bytes: bytes,
    artigo: dict,
    categoria_id: int,
) -> dict:
    auth = (wp_user, wp_app_password)

    # Nome de ficheiro único por artigo (corrigido 2026-09-07): usar sempre o
    # mesmo nome literal "capa-cloudflare.jpg" fazia o WordPress numerar
    # automaticamente cada upload (capa-cloudflare.jpg, -1.jpg, -2.jpg, ...).
    # Isto por si só não é um bug, MAS torna esses números reutilizáveis: se
    # um ficheiro antigo com um desses nomes for apagado mais tarde (ex. pelo
    # utilitário `corrigir_imagem_post.py`), o WordPress pode voltar a atribuir
    # o MESMO nome a uma imagem completamente nova e sem relação — e se algum
    # nível de cache/CDN ainda tiver essa URL em cache, continua a servir a
    # imagem antiga (errada) por baixo do nome novo. Foi exactamente isto que
    # aconteceu com o artigo "Apresentadora de TV no Egito..." (ver
    # claude/2026-09-06-verificacao-imagem-e-correcao-post.md): a imagem
    # errada de um artigo já corrigido reapareceu, através de cache, num
    # artigo completamente diferente que por coincidência recebeu o mesmo
    # nome de ficheiro reciclado. Usar um nome derivado do slug do próprio
    # artigo (que já é único, graças à deduplicação) elimina de vez a
    # possibilidade de reutilização de nome.
    slug = artigo["slug"].strip() or slugify(artigo["titulo_post"])
    slug_seguro = slugify(slug) or "artigo"
    nome_ficheiro = f"capa-{slug_seguro}.jpg"[:150]

    # 1) Upload da imagem de capa
    media_resp = requests.post(
        f"{base_url.rstrip('/')}/wp-json/wp/v2/media",
        auth=auth,
        headers={
            "Content-Disposition": f"attachment; filename={nome_ficheiro}",
            "Content-Type": "image/jpeg",
            "User-Agent": WP_USER_AGENT,
        },
        data=imagem_bytes,
        timeout=HTTP_TIMEOUT,
    )
    if not media_resp.ok:
        raise RuntimeError(f"WordPress (media) devolveu {media_resp.status_code}: {media_resp.text[:500]}")
    media_data = media_resp.json()
    media_id = media_data["id"]

    # Título/alt text da imagem (equivalente ao WordPress "Create a Media Item" no Make)
    requests.post(
        f"{base_url.rstrip('/')}/wp-json/wp/v2/media/{media_id}",
        auth=auth,
        headers={"User-Agent": WP_USER_AGENT},
        json={"title": artigo["titulo_post"], "alt_text": artigo["alt_text"]},
        timeout=HTTP_TIMEOUT,
    )

    # 2) Criação do post (slug já calculado acima, para o nome do ficheiro)
    post_resp = requests.post(
        f"{base_url.rstrip('/')}/wp-json/wp/v2/posts",
        auth=auth,
        headers={"User-Agent": WP_USER_AGENT},
        json={
            "title": artigo["titulo_post"],
            "content": artigo["artigo_html"],
            "excerpt": artigo["meta_description"],
            "slug": slug,
            "status": "publish",
            "categories": [categoria_id],
            "featured_media": media_id,
        },
        timeout=HTTP_TIMEOUT,
    )
    if not post_resp.ok:
        raise RuntimeError(f"WordPress (post) devolveu {post_resp.status_code}: {post_resp.text[:500]}")

    return post_resp.json()

# ---------------------------------------------------------------------------
# Orquestração principal
# ---------------------------------------------------------------------------

def processar_site(
    cfg: dict,
    site: dict,
    state: dict,
    gemini_key: str,
    cf_account_id: str,
    cf_token: str,
    wp_user: str,
    wp_app_password: str,
    dry_run: bool,
) -> dict:
    """Processa TODAS as fontes RSS de um site em conjunto, para poder aplicar
    o alvo de publicações por CATEGORIA (ver `alvo_publicacoes_por_categoria`
    no config.yaml) através de todas as fontes — em vez de decidir fonte a
    fonte, que não tem em conta a categoria real do artigo (só conhecida
    depois de a Gemini reescrever o texto)."""
    resumo = {"site": site["id"], "publicados": 0, "ignorados": 0, "erros": 0, "descartados_por_imagem": 0}
    definicoes = cfg["definicoes_globais"]

    # 1) Recolher itens elegíveis de cada fonte, e intercalá-los (round-robin)
    # para dar a cada fonte uma hipótese igual de contribuir.
    itens_por_fonte = []
    for fonte in site["fontes"]:
        try:
            itens_brutos = fetch_rss_items(fonte["rss_url"])
        except Exception as exc:  # noqa: BLE001
            log(f"[{site['id']}/{fonte['id']}] ERRO ao ler RSS: {exc}")
            resumo["erros"] += 1
            continue

        itens = filtrar_itens_recentes(itens_brutos, definicoes["rss_lookback_hours"], definicoes["max_items_por_fonte"])
        log(f"[{site['id']}/{fonte['id']}] {len(itens)} item(ns) elegível(eis) encontrados no RSS.")
        for item in itens:
            item["_fonte"] = fonte
        itens_por_fonte.append(itens)

    itens = intercalar_por_fonte(itens_por_fonte)

    # 2) Preparar deduplicação (por título, contra publicações recentes deste site).
    limiar = definicoes["duplicidade_limiar_semelhanca"]
    janela_dias = definicoes["duplicidade_janela_dias"]
    limite_dedupe = datetime.now(timezone.utc) - timedelta(days=janela_dias)
    titulos_recentes = [
        p["titulo_original"]
        for p in state["publicados"]
        if p["site"] == site["id"] and datetime.fromisoformat(p["publicado_em"]) >= limite_dedupe
    ]

    # 3) Preparar contagem de publicações por categoria NESTA execução.
    # Usa o ID de categoria do WordPress como chave (mais robusto do que o
    # nome de texto devolvido pela Gemini, que é o que decide o ID).
    alvo_por_categoria = definicoes.get("alvo_publicacoes_por_categoria", 2)
    limite_seguranca_gemini = definicoes.get("limite_seguranca_gemini_por_execucao", 14)
    categoria_map = site["categoria_map"]
    ids_categoria_possiveis = set(categoria_map.values()) | {site["categoria_fallback"]}
    contagem_por_categoria = {cat_id: 0 for cat_id in ids_categoria_possiveis}
    chamadas_gemini = 0

    def todas_categorias_completas() -> bool:
        return all(contagem_por_categoria[cat_id] >= alvo_por_categoria for cat_id in ids_categoria_possiveis)

    for item in itens:
        fonte = item["_fonte"]

        if todas_categorias_completas():
            log(f"[{site['id']}] Todas as categorias já atingiram o alvo de {alvo_por_categoria} publicações nesta execução — a parar.")
            break

        if chamadas_gemini >= limite_seguranca_gemini:
            log(f"[{site['id']}] Limite de segurança de {limite_seguranca_gemini} chamadas à Gemini atingido nesta execução — a parar.")
            break

        if any(titulos_semelhantes(item["titulo"], t, limiar) for t in titulos_recentes):
            log(f"[{site['id']}/{fonte['id']}] Ignorado (duplicado/semelhante a publicação recente): {item['titulo']!r}")
            resumo["ignorados"] += 1
            continue

        try:
            chamadas_gemini += 1
            artigo = gerar_artigo_gemini(cfg, gemini_key, item["titulo"], item["descricao"], item["nome_fonte"] or fonte["nome"])

            categoria_id = categoria_map.get(artigo["categoria"], site["categoria_fallback"])

            if contagem_por_categoria.get(categoria_id, 0) >= alvo_por_categoria:
                log(
                    f"[{site['id']}/{fonte['id']}] Ignorado (categoria {artigo['categoria']!r} já tem "
                    f"{alvo_por_categoria} publicação(ões) nesta execução): {item['titulo']!r}"
                )
                resumo["ignorados"] += 1
                continue

            imagem_bytes, motivo_reprovacao = gerar_imagem_validada(
                cfg, cf_account_id, cf_token, gemini_key, artigo["alt_text"]
            )
            if imagem_bytes is None:
                log(
                    f"[{site['id']}/{fonte['id']}] Artigo NÃO publicado — imagem reprovada repetidamente "
                    f"na verificação de qualidade ({motivo_reprovacao!r}): {artigo['titulo_post']!r}"
                )
                resumo["descartados_por_imagem"] += 1
                continue

            if dry_run:
                log(f"[{site['id']}/{fonte['id']}] DRY-RUN — não publicado: {artigo['titulo_post']!r} (categoria={artigo['categoria']} -> id {categoria_id})")
            else:
                resultado = publicar_no_wordpress(
                    site["wordpress_base_url"], wp_user, wp_app_password, imagem_bytes, artigo, categoria_id
                )
                log(f"[{site['id']}/{fonte['id']}] Publicado: {artigo['titulo_post']!r} -> {resultado.get('link')}")

            titulos_recentes.append(item["titulo"])
            contagem_por_categoria[categoria_id] = contagem_por_categoria.get(categoria_id, 0) + 1
            state["publicados"].append(
                {
                    "site": site["id"],
                    "fonte": fonte["id"],
                    "titulo_original": item["titulo"],
                    "titulo_post": artigo["titulo_post"],
                    "slug": artigo["slug"],
                    "categoria_id": categoria_id,
                    "publicado_em": datetime.now(timezone.utc).isoformat(),
                    "dry_run": dry_run,
                }
            )
            resumo["publicados"] += 1

        except Exception as exc:  # noqa: BLE001
            log(f"[{site['id']}/{fonte['id']}] ERRO ao processar item {item['titulo']!r}: {exc}")
            resumo["erros"] += 1
            continue

        # Pequena pausa entre artigos para não sobrecarregar as APIs.
        time.sleep(2)

    faltam = {
        cat_nome: alvo_por_categoria - contagem_por_categoria.get(cat_id, 0)
        for cat_nome, cat_id in categoria_map.items()
        if contagem_por_categoria.get(cat_id, 0) < alvo_por_categoria
    }
    if faltam:
        log(f"[{site['id']}] Categorias que não atingiram o alvo nesta execução (faltam candidatos elegíveis no RSS): {faltam}")

    return resumo

def processar_site_pesquisa(
    cfg: dict,
    site: dict,
    state: dict,
    gemini_key: str,
    cf_account_id: str,
    cf_token: str,
    wp_user: str,
    wp_app_password: str,
    dry_run: bool,
) -> dict:
    """Processa um site em modo 'pesquisa_gemini' (`site['modo']`): em vez de
    ler RSS, pede à Gemini para pesquisar no Google (grounding) e escrever um
    artigo original e REAL para cada tema em `site['temas_pesquisa']`. Usado
    para sites sem fonte RSS própria (ex. pano-rama.online)."""
    resumo = {"site": site["id"], "publicados": 0, "ignorados": 0, "erros": 0, "descartados_por_imagem": 0}
    definicoes = cfg["definicoes_globais"]
    alvo_por_tema = site.get("alvo_publicacoes_por_tema", 1)

    limiar = definicoes["duplicidade_limiar_semelhanca"]
    janela_dias = definicoes["duplicidade_janela_dias"]
    limite_dedupe = datetime.now(timezone.utc) - timedelta(days=janela_dias)
    titulos_recentes_site = [
        p["titulo_post"]
        for p in state["publicados"]
        if p["site"] == site["id"] and datetime.fromisoformat(p["publicado_em"]) >= limite_dedupe
    ]

    categoria_map = site["categoria_map"]

    for tema in site["temas_pesquisa"]:
        for _ in range(alvo_por_tema):
            try:
                artigo, fonte_ou_motivo = gerar_artigo_gemini_pesquisa(cfg, gemini_key, tema, titulos_recentes_site)

                if artigo is None:
                    log(f"[{site['id']}/{tema['id']}] Ignorado — {fonte_ou_motivo}.")
                    resumo["ignorados"] += 1
                    continue

                if any(titulos_semelhantes(artigo["titulo_post"], t, limiar) for t in titulos_recentes_site):
                    log(f"[{site['id']}/{tema['id']}] Ignorado (semelhante a publicação recente): {artigo['titulo_post']!r}")
                    resumo["ignorados"] += 1
                    continue

                categoria_id = categoria_map.get(tema["categoria"], site["categoria_fallback"])

                imagem_bytes, motivo_reprovacao = gerar_imagem_validada(
                    cfg, cf_account_id, cf_token, gemini_key, artigo["alt_text"]
                )
                if imagem_bytes is None:
                    log(
                        f"[{site['id']}/{tema['id']}] Artigo NÃO publicado — imagem reprovada repetidamente "
                        f"na verificação de qualidade ({motivo_reprovacao!r}): {artigo['titulo_post']!r}"
                    )
                    resumo["descartados_por_imagem"] += 1
                    continue

                if dry_run:
                    log(f"[{site['id']}/{tema['id']}] DRY-RUN — não publicado: {artigo['titulo_post']!r} (fonte: {fonte_ou_motivo})")
                else:
                    resultado = publicar_no_wordpress(
                        site["wordpress_base_url"], wp_user, wp_app_password, imagem_bytes, artigo, categoria_id
                    )
                    log(f"[{site['id']}/{tema['id']}] Publicado: {artigo['titulo_post']!r} -> {resultado.get('link')} (fonte: {fonte_ou_motivo})")

                titulos_recentes_site.append(artigo["titulo_post"])
                state["publicados"].append(
                    {
                        "site": site["id"],
                        "fonte": tema["id"],
                        "titulo_original": artigo["titulo_post"],
                        "titulo_post": artigo["titulo_post"],
                        "slug": artigo["slug"],
                        "categoria_id": categoria_id,
                        "fonte_pesquisa_url": fonte_ou_motivo,
                        "publicado_em": datetime.now(timezone.utc).isoformat(),
                        "dry_run": dry_run,
                    }
                )
                resumo["publicados"] += 1

            except Exception as exc:  # noqa: BLE001
                log(f"[{site['id']}/{tema['id']}] ERRO ao processar tema: {exc}")
                resumo["erros"] += 1
                continue

            # Pequena pausa entre artigos para não sobrecarregar as APIs.
            time.sleep(2)

    return resumo

def main() -> int:
    parser = argparse.ArgumentParser(description="Novimz Auto-Publisher (Python)")
    parser.add_argument("--dry-run", action="store_true", help="Corre todo o pipeline sem publicar no WordPress.")
    args = parser.parse_args()

    cfg = load_config()
    state = load_state()

    gemini_key = env_required("GEMINI_API_KEY")
    cf_account_id = env_required("CLOUDFLARE_ACCOUNT_ID")
    cf_token = env_required("CLOUDFLARE_API_TOKEN")

    resumos = []
    for site in cfg["sites"]:
        wp_user = env_required(site["wordpress_user_env"])
        wp_app_password = env_required(site["wordpress_app_password_env"])

        if site.get("modo") == "pesquisa_gemini":
            resumo = processar_site_pesquisa(
                cfg, site, state, gemini_key, cf_account_id, cf_token, wp_user, wp_app_password, args.dry_run
            )
        else:
            resumo = processar_site(
                cfg, site, state, gemini_key, cf_account_id, cf_token, wp_user, wp_app_password, args.dry_run
            )
        resumos.append(resumo)

    state = prune_state(state, cfg["definicoes_globais"]["duplicidade_janela_dias"])
    save_state(state)

    total_publicados = sum(r["publicados"] for r in resumos)
    total_ignorados = sum(r["ignorados"] for r in resumos)
    total_erros = sum(r["erros"] for r in resumos)
    total_descartados_imagem = sum(r.get("descartados_por_imagem", 0) for r in resumos)
    log(
        f"RESUMO DA EXECUÇÃO: {total_publicados} publicado(s), {total_ignorados} ignorado(s), "
        f"{total_descartados_imagem} descartado(s) por imagem reprovada, {total_erros} erro(s)."
    )

    # Não falha a execução do workflow por erros pontuais num artigo (para não
    # gerar alertas de falha constantes por causas transitórias de rede/API) —
    # só falha se NADA correu e houve pelo menos um erro (sinal de problema
    # estrutural, ex. credencial errada).
    if total_publicados == 0 and total_erros > 0 and total_ignorados == 0:
        log("Nenhum artigo publicado e só houve erros — a terminar com código de saída 1.")
        return 1

    return 0

if __name__ == "__main__":
    sys.exit(main())

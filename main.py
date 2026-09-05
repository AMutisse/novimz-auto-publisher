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
    python main.py               # corre todos os sites/fontes do config.yaml
    python main.py --dry-run     # faz tudo menos publicar no WordPress
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


# ---------------------------------------------------------------------------
# Google Gemini AI (texto)
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

    # 1) Upload da imagem de capa
    media_resp = requests.post(
        f"{base_url.rstrip('/')}/wp-json/wp/v2/media",
        auth=auth,
        headers={
            "Content-Disposition": "attachment; filename=capa-cloudflare.jpg",
            "Content-Type": "image/jpeg",
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
        json={"title": artigo["titulo_post"], "alt_text": artigo["alt_text"]},
        timeout=HTTP_TIMEOUT,
    )

    # 2) Criação do post
    slug = artigo["slug"].strip() or slugify(artigo["titulo_post"])
    post_resp = requests.post(
        f"{base_url.rstrip('/')}/wp-json/wp/v2/posts",
        auth=auth,
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

def processar_fonte(
    cfg: dict,
    site: dict,
    fonte: dict,
    state: dict,
    gemini_key: str,
    cf_account_id: str,
    cf_token: str,
    wp_user: str,
    wp_app_password: str,
    dry_run: bool,
) -> dict:
    resumo = {"site": site["id"], "fonte": fonte["id"], "publicados": 0, "ignorados": 0, "erros": 0}
    definicoes = cfg["definicoes_globais"]

    try:
        itens = fetch_rss_items(fonte["rss_url"])
    except Exception as exc:  # noqa: BLE001
        log(f"[{site['id']}/{fonte['id']}] ERRO ao ler RSS: {exc}")
        resumo["erros"] += 1
        return resumo

    itens = filtrar_itens_recentes(itens, definicoes["rss_lookback_hours"], definicoes["max_items_por_fonte"])
    log(f"[{site['id']}/{fonte['id']}] {len(itens)} item(ns) elegível(eis) encontrados no RSS.")

    limiar = definicoes["duplicidade_limiar_semelhanca"]
    janela_dias = definicoes["duplicidade_janela_dias"]
    limite_dedupe = datetime.now(timezone.utc) - timedelta(days=janela_dias)
    titulos_recentes = [
        p["titulo_original"]
        for p in state["publicados"]
        if p["site"] == site["id"] and datetime.fromisoformat(p["publicado_em"]) >= limite_dedupe
    ]

    for item in itens:
        if any(titulos_semelhantes(item["titulo"], t, limiar) for t in titulos_recentes):
            log(f"[{site['id']}/{fonte['id']}] Ignorado (duplicado/semelhante a publicação recente): {item['titulo']!r}")
            resumo["ignorados"] += 1
            continue

        try:
            artigo = gerar_artigo_gemini(cfg, gemini_key, item["titulo"], item["descricao"], item["nome_fonte"] or fonte["nome"])

            categoria_map = site["categoria_map"]
            categoria_id = categoria_map.get(artigo["categoria"], site["categoria_fallback"])

            imagem_bytes = gerar_imagem_cloudflare(cfg, cf_account_id, cf_token, artigo["alt_text"])

            if dry_run:
                log(f"[{site['id']}/{fonte['id']}] DRY-RUN — não publicado: {artigo['titulo_post']!r} (categoria={artigo['categoria']} -> id {categoria_id})")
            else:
                resultado = publicar_no_wordpress(
                    site["wordpress_base_url"], wp_user, wp_app_password, imagem_bytes, artigo, categoria_id
                )
                log(f"[{site['id']}/{fonte['id']}] Publicado: {artigo['titulo_post']!r} -> {resultado.get('link')}")

            titulos_recentes.append(item["titulo"])
            state["publicados"].append(
                {
                    "site": site["id"],
                    "fonte": fonte["id"],
                    "titulo_original": item["titulo"],
                    "titulo_post": artigo["titulo_post"],
                    "slug": artigo["slug"],
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

        for fonte in site["fontes"]:
            resumo = processar_fonte(
                cfg, site, fonte, state, gemini_key, cf_account_id, cf_token, wp_user, wp_app_password, args.dry_run
            )
            resumos.append(resumo)

    state = prune_state(state, cfg["definicoes_globais"]["duplicidade_janela_dias"])
    save_state(state)

    total_publicados = sum(r["publicados"] for r in resumos)
    total_ignorados = sum(r["ignorados"] for r in resumos)
    total_erros = sum(r["erros"] for r in resumos)
    log(f"RESUMO DA EXECUÇÃO: {total_publicados} publicado(s), {total_ignorados} ignorado(s), {total_erros} erro(s).")

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

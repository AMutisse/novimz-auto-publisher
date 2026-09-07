#!/usr/bin/env python3
"""
Novimz Auto-Publisher — correcção manual da imagem de destaque de um post
==========================================================================

Utilitário para substituir a imagem de destaque de um artigo JÁ PUBLICADO,
quando ela sair com um defeito (ex. figura humana sem cabeça, mãos
deformadas, texto ilegível em letreiros — falhas conhecidas do modelo
gratuito Cloudflare FLUX.1-schnell). Gera uma imagem nova (com a mesma
verificação de qualidade pela Gemini usada no pipeline normal, ver
`gerar_imagem_validada` em main.py), substitui-a no WordPress, e só depois
apaga a imagem antiga da Biblioteca de Multimédia.

Corre sempre através do workflow do GitHub Actions
`.github/workflows/corrigir_imagem.yml` (usa os secrets já configurados —
nunca precisa de nenhuma credencial nova). Não é suposto correr localmente
sem as variáveis de ambiente todas definidas.

Uso (dentro do workflow, via variáveis de ambiente):
    SITE_ID=novimz POST_SLUG="governo-impulsiona-72-mil-jovens..." python corrigir_imagem_post.py
    ALT_TEXT_OVERRIDE="..." (opcional) — substitui o texto usado para gerar a nova imagem;
        por omissão reaproveita o alt_text/título actual do post.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import requests

from main import (
    WP_USER_AGENT,
    env_required,
    gerar_imagem_validada,
    load_config,
    log,
    slugify,
)


def buscar_post_por_slug(base_url: str, auth: tuple[str, str], slug: str) -> dict:
    resp = requests.get(
        f"{base_url.rstrip('/')}/wp-json/wp/v2/posts",
        auth=auth,
        headers={"User-Agent": WP_USER_AGENT},
        params={"slug": slug, "status": "publish,draft,pending,future"},
        timeout=60,
    )
    if not resp.ok:
        raise RuntimeError(f"WordPress (procurar post) devolveu {resp.status_code}: {resp.text[:500]}")
    posts = resp.json()
    if not posts:
        raise RuntimeError(f"Nenhum post encontrado com o slug {slug!r}.")
    return posts[0]


def buscar_media(base_url: str, auth: tuple[str, str], media_id: int) -> dict | None:
    if not media_id:
        return None
    resp = requests.get(
        f"{base_url.rstrip('/')}/wp-json/wp/v2/media/{media_id}",
        auth=auth,
        headers={"User-Agent": WP_USER_AGENT},
        timeout=60,
    )
    if not resp.ok:
        return None
    return resp.json()


def enviar_nova_imagem(
    base_url: str, auth: tuple[str, str], imagem_bytes: bytes, titulo: str, alt_text: str, post_slug: str
) -> int:
    # Nome de ficheiro único por correcção (corrigido 2026-09-07): usar sempre
    # o mesmo nome literal "capa-cloudflare-corrigida.jpg" corria o mesmo
    # risco documentado em main.py — um nome reutilizado por várias correcções
    # pode acabar por colidir com um nome antigo já apagado e servido em cache
    # por um CDN, fazendo a imagem errada de UM artigo reaparecer, através de
    # cache, num artigo completamente diferente (foi isto que aconteceu com o
    # artigo "Apresentadora de TV no Egito...", ver
    # claude/2026-09-06-verificacao-imagem-e-correcao-post.md). Incluir o slug
    # do post e um timestamp garante que este nome nunca colide com nenhum
    # outro, presente ou futuro.
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    slug_seguro = slugify(post_slug) or "post"
    nome_ficheiro = f"capa-{slug_seguro}-corrigida-{timestamp}.jpg"[:150]

    media_resp = requests.post(
        f"{base_url.rstrip('/')}/wp-json/wp/v2/media",
        auth=auth,
        headers={
            "Content-Disposition": f"attachment; filename={nome_ficheiro}",
            "Content-Type": "image/jpeg",
            "User-Agent": WP_USER_AGENT,
        },
        data=imagem_bytes,
        timeout=60,
    )
    if not media_resp.ok:
        raise RuntimeError(f"WordPress (upload da imagem nova) devolveu {media_resp.status_code}: {media_resp.text[:500]}")
    media_id = media_resp.json()["id"]

    requests.post(
        f"{base_url.rstrip('/')}/wp-json/wp/v2/media/{media_id}",
        auth=auth,
        headers={"User-Agent": WP_USER_AGENT},
        json={"title": titulo, "alt_text": alt_text},
        timeout=60,
    )
    return media_id


def actualizar_featured_media(base_url: str, auth: tuple[str, str], post_id: int, media_id: int) -> None:
    resp = requests.post(
        f"{base_url.rstrip('/')}/wp-json/wp/v2/posts/{post_id}",
        auth=auth,
        headers={"User-Agent": WP_USER_AGENT},
        json={"featured_media": media_id},
        timeout=60,
    )
    if not resp.ok:
        raise RuntimeError(f"WordPress (actualizar post) devolveu {resp.status_code}: {resp.text[:500]}")


def apagar_media(base_url: str, auth: tuple[str, str], media_id: int) -> bool:
    resp = requests.delete(
        f"{base_url.rstrip('/')}/wp-json/wp/v2/media/{media_id}",
        auth=auth,
        headers={"User-Agent": WP_USER_AGENT},
        params={"force": "true"},
        timeout=60,
    )
    return resp.ok


def main() -> int:
    site_id = os.environ.get("SITE_ID", "novimz")
    post_slug = env_required("POST_SLUG")
    alt_text_override = os.environ.get("ALT_TEXT_OVERRIDE", "").strip()

    cfg = load_config()
    site = next((s for s in cfg["sites"] if s["id"] == site_id), None)
    if site is None:
        raise RuntimeError(f"Site {site_id!r} não encontrado em config.yaml.")

    gemini_key = env_required("GEMINI_API_KEY")
    cf_account_id = env_required("CLOUDFLARE_ACCOUNT_ID")
    cf_token = env_required("CLOUDFLARE_API_TOKEN")
    wp_user = env_required(site["wordpress_user_env"])
    wp_app_password = env_required(site["wordpress_app_password_env"])
    auth = (wp_user, wp_app_password)
    base_url = site["wordpress_base_url"]

    log(f"A procurar o post com slug {post_slug!r} em {base_url}...")
    post = buscar_post_por_slug(base_url, auth, post_slug)
    post_id = post["id"]
    titulo = post["title"]["rendered"]
    media_antiga_id = post.get("featured_media") or 0
    log(f"Post encontrado: id={post_id}, título={titulo!r}, imagem de destaque actual id={media_antiga_id}.")

    if alt_text_override:
        alt_text = alt_text_override
    else:
        media_antiga = buscar_media(base_url, auth, media_antiga_id)
        alt_text = (media_antiga or {}).get("alt_text", "").strip() or titulo
    log(f"Prompt/alt_text usado para gerar a nova imagem: {alt_text!r}")

    imagem_bytes, motivo = gerar_imagem_validada(cfg, cf_account_id, cf_token, gemini_key, alt_text)
    if imagem_bytes is None:
        log(f"ERRO: não foi possível gerar uma imagem aprovada na verificação de qualidade ({motivo!r}). Nada foi alterado no post.")
        return 1

    log("Imagem nova gerada e aprovada — a enviar para o WordPress...")
    media_nova_id = enviar_nova_imagem(base_url, auth, imagem_bytes, titulo, alt_text, post_slug)
    log(f"Imagem nova criada: id={media_nova_id}. A actualizar o post para usar esta imagem como destaque...")
    actualizar_featured_media(base_url, auth, post_id, media_nova_id)
    log(f"Post {post_id} actualizado com sucesso — imagem de destaque agora é a id={media_nova_id}.")

    if media_antiga_id and media_antiga_id != media_nova_id:
        if apagar_media(base_url, auth, media_antiga_id):
            log(f"Imagem antiga (id={media_antiga_id}) apagada da Biblioteca de Multimédia.")
        else:
            log(f"AVISO: não foi possível apagar a imagem antiga (id={media_antiga_id}) — fica na Biblioteca de Multimédia, sem uso.")

    return 0


if __name__ == "__main__":
    sys.exit(main())

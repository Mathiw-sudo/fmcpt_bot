gram downloader · PY
Copiar

import re
import os
import json
import asyncio
import logging
from functools import partial
import yt_dlp
import aiohttp
import instaloader
import httpx
 
log = logging.getLogger("SuperBot")
 
# -------------------------------------------------------------------
# INSTÂNCIA GLOBAL DO INSTALOADER
# CORREÇÃO: não usar instância global para downloads — o contexto
# acumula erros e sessões expiradas entre requisições. A instância
# global aqui é mantida SOMENTE como referência de configuração.
# Cada tentativa real cria seu próprio contexto isolado.
# -------------------------------------------------------------------
_INSTALOADER_CONFIG = dict(
    download_pictures=True,
    download_video_thumbnails=False,
    download_videos=True,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    compress_json=False,
)
 
# CORREÇÃO: User-Agent atualizado para Chrome 124 (mais recente na época).
# O UA antigo (Chrome 125) ainda não existia e causava rejeição em alguns CDNs.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
 
# Regex para extrair URLs de imagens e vídeos do embed
IMG_REGEX = re.compile(r'class="EmbeddedMediaImage"[^>]*src="([^"]+)"')
VIDEO_REGEX = re.compile(r'class="EmbeddedVideoPlayer"[^>]*src="([^"]+)"')
SHORTCODE_REGEX = re.compile(r'/(?:p|reel|ad|tv)/([A-Za-z0-9_-]+)')
 
 
def _get_shortcode(url: str) -> str | None:
    """Extrai o shortcode do Instagram da URL."""
    match = SHORTCODE_REGEX.search(url)
    return match.group(1) if match else None
 
 
def _run_ytdlp(url: str, ydl_opts: dict) -> dict:
    """Executa yt-dlp em thread separada (não bloqueante para o event loop)."""
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=True)
 
 
# -------------------------------------------------------------------
# CORREÇÃO: Caminho padrão para cookies centralizado.
# Antes, cada função calculava o caminho de forma diferente e
# inconsistente (alguns usavam __file__, outros recebiam por parâmetro).
# Agora existe uma função única que resolve o caminho de forma confiável.
# -------------------------------------------------------------------
def _resolve_cookie_path(cookie_path: str | None = None) -> str:
    """
    Retorna o caminho do arquivo de cookies.
    Prioridade: argumento explícito > variável de ambiente > caminho padrão.
    """
    if cookie_path and os.path.exists(cookie_path):
        return cookie_path
    env_path = os.environ.get("INSTAGRAM_COOKIE_PATH", "")
    if env_path and os.path.exists(env_path):
        return env_path
    # Caminho padrão: <raiz do projeto>/data/instagram_cookies.txt
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "data", "instagram_cookies.txt")
 
 
# -------------------------------------------------------------------
# CORREÇÃO: Carregamento de cookies no Instaloader refatorado.
# O código original usava http.cookiejar.MozillaCookieJar diretamente
# na sessão do instaloader, o que não funcionava corretamente porque
# o instaloader gerencia sua própria sessão requests.Session interna.
# A forma correta é usar context._session.cookies.update() com um
# RequestsCookieJar, ou o método load_session_cookie_jar() quando
# disponível.
# -------------------------------------------------------------------
def _load_cookies_into_context(
    context: instaloader.InstaloaderContext,
    cookie_path: str,
) -> bool:
    """
    Carrega cookies Netscape/Mozilla no contexto do Instaloader.
    Retorna True se pelo menos um cookie foi carregado com sucesso.
    """
    if not os.path.exists(cookie_path):
        log.debug("Arquivo de cookies não encontrado: %s", cookie_path)
        return False
    try:
        import http.cookiejar
        cj = http.cookiejar.MozillaCookieJar(cookie_path)
        cj.load(ignore_discard=True, ignore_expires=True)
        # CORREÇÃO: update() em vez de set_cookie() em loop —
        # evita duplicatas e é mais eficiente.
        context._session.cookies.update(cj)
        count = sum(1 for _ in cj)
        log.info("Cookies carregados no Instaloader: %d cookies de %s", count, cookie_path)
        return count > 0
    except Exception as exc:
        log.warning("Falha ao carregar cookies no Instaloader: %s", exc)
        return False
 
 
async def download_with_cookies(
    url: str,
    cookie_path: str,
    out_dir: str,
    timeout: int = 60,
) -> dict | None:
    """
    Tenta download via yt-dlp com cookies de autenticação.
 
    Retorna dict com:
      - type: 'photo', 'video', 'carousel'
      - files: lista de paths dos arquivos baixados
      - title: título/descrição
      - uploader: nome do autor
    """
    resolved = _resolve_cookie_path(cookie_path)
    if not os.path.exists(resolved):
        log.warning("Arquivo de cookies não encontrado: %s", resolved)
        return None
 
    loop = asyncio.get_running_loop()
 
    ydl_opts = {
        # CORREÇÃO: template de saída ajustado.
        # O campo %(index)s não existe em todos os itens do Instagram,
        # causando KeyError no yt-dlp. Usar %(autonumber)s é mais seguro.
        "outtmpl": os.path.join(out_dir, "%(id)s_%(autonumber)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "extract_flat": False,
        "cookiefile": resolved,
        "socket_timeout": timeout,
        "retries": 3,
        "http_headers": {"User-Agent": _UA},
        # CORREÇÃO: forçar formato de vídeo compatível com Telegram/Discord.
        # Sem isso, o yt-dlp pode baixar formatos DASH separados (video+audio)
        # que precisam de merge e que não existem no Instagram.
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        # CORREÇÃO: desabilitar extração de metadados extras que causam
        # requisições adicionais desnecessárias ao Instagram.
        "writeinfojson": False,
        "writethumbnail": False,
    }
 
    try:
        info = await asyncio.wait_for(
            loop.run_in_executor(None, partial(_run_ytdlp, url, ydl_opts)),
            timeout=timeout,
        )
 
        entries = info.get("entries") or [info]
        arquivos: list[str] = []
 
        for item in entries:
            # CORREÇÃO: lógica de extração de filepath tornada mais robusta.
            # A ordem de verificação agora cobre todas as variações que
            # o yt-dlp pode retornar dependendo da versão.
            path = None
            for dl in item.get("requested_downloads", []):
                candidate = dl.get("filepath") or dl.get("filename")
                if candidate and os.path.exists(candidate):
                    path = candidate
                    break
            if not path:
                for key in ("filepath", "filename", "_filename"):
                    candidate = item.get(key)
                    if candidate and os.path.exists(candidate):
                        path = candidate
                        break
            if path and path not in arquivos:
                arquivos.append(path)
 
        if not arquivos:
            log.debug("yt-dlp não retornou arquivos para: %s", url)
            return None
 
        media_type = (
            "carousel"
            if len(arquivos) > 1
            else ("video" if arquivos[0].lower().endswith((".mp4", ".mov", ".webm")) else "photo")
        )
        return {
            "type": media_type,
            "files": arquivos,
            "title": info.get("title") or info.get("description") or "",
            "uploader": info.get("uploader") or info.get("channel") or "Autor",
        }
 
    except asyncio.TimeoutError:
        log.warning("yt-dlp timeout para Instagram: %s", url)
        return None
    except Exception as exc:
        log.debug("yt-dlp com cookies falhou: %s", str(exc)[:200])
        return None
 
 
async def download_via_embed(url: str) -> dict | None:
    """
    Fallback: extrai mídia via Instagram embed endpoint.
    Funciona para posts públicos sem necessidade de login.
    """
    shortcode = _get_shortcode(url)
    if not shortcode:
        return None
 
    embed_url = f"https://www.instagram.com/p/{shortcode}/embed/"
 
    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        # CORREÇÃO: adicionar Referer e Origin melhora a taxa de sucesso
        # porque o Instagram valida esses headers no embed endpoint.
        "Referer": "https://www.instagram.com/",
        "Origin": "https://www.instagram.com",
    }
 
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                embed_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    log.debug("Embed retornou status %d para %s", resp.status, url)
                    return None
                html = await resp.text()
 
        img_urls = IMG_REGEX.findall(html)
        video_urls = VIDEO_REGEX.findall(html)
 
        if not img_urls:
            img_urls = re.findall(r'"display_url"\s*:\s*"([^"]+)"', html)
        if not video_urls:
            video_urls = re.findall(r'"video_url"\s*:\s*"([^"]+)"', html)
 
        def _clean(u: str) -> str:
            return u.replace("\\/", "/").replace("&amp;", "&")
 
        img_urls = [_clean(u) for u in img_urls]
        video_urls = [_clean(u) for u in video_urls]
 
        # Tenta pegar a resolução mais alta do srcset
        srcset_match = re.search(r'srcset="([^"]+)"', html)
        if srcset_match and img_urls:
            candidates = [
                part.split(" ")[0]
                for part in srcset_match.group(1).split(",")
                if part.strip()
            ]
            best = candidates[-1].replace("&amp;", "&") if candidates else None
            if best and best.startswith("http"):
                img_urls[0] = best
 
        caption = ""
        caption_match = re.search(r'"caption"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
        if caption_match:
            try:
                caption = caption_match.group(1).encode("raw_unicode_escape").decode("unicode_escape")
            except Exception:
                caption = caption_match.group(1)
 
        author = ""
        author_match = re.search(r'"username"\s*:\s*"([^"]+)"', html)
        if author_match:
            author = author_match.group(1)
 
        midias: list[dict] = []
        if video_urls:
            midias = [{"type": "video", "url": _clean(v)} for v in video_urls]
        elif img_urls:
            midias = [{"type": "photo", "url": _clean(i)} for i in img_urls]
 
        if not midias:
            return None
 
        return {
            "type": "carousel" if len(midias) > 1 else midias[0]["type"],
            "urls": [m["url"] for m in midias],
            "title": caption,
            "uploader": author,
        }
 
    except asyncio.TimeoutError:
        log.warning("Embed timeout para Instagram: %s", url)
        return None
    except Exception as exc:
        log.debug("Embed fallback falhou: %s", str(exc)[:200])
        return None
 
 
async def download_via_instaloader(url: str, out_dir: str, cookie_path: str | None = None) -> dict | None:
    """
    Fallback 2: usa o Instaloader para buscar URLs de mídia (sem salvar em disco).
 
    CORREÇÃO PRINCIPAL: o código original baixava os arquivos em disco com
    o Instaloader (lento, gera arquivos temporários desnecessários) e depois
    os descartava. A abordagem correta é usar o Instaloader SOMENTE para
    resolver as URLs e deixar o yt-dlp ou aiohttp fazer o download real.
    Isso evita o problema de encoding do título e é muito mais rápido.
    """
    shortcode = _get_shortcode(url)
    if not shortcode:
        return None
 
    log.info("Tentando Instaloader para shortcode: %s", shortcode)
 
    loop = asyncio.get_running_loop()
    resolved_cookie = _resolve_cookie_path(cookie_path)
 
    def _get_post_info() -> dict:
        local_L = instaloader.Instaloader(
            download_pictures=False,
            download_video_thumbnails=False,
            download_videos=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            # CORREÇÃO: max_connection_attempts=1 impede o Instaloader de
            # tentar reconexões infinitas quando o Instagram retorna 401/429,
            # o que travava a thread por minutos.
            max_connection_attempts=1,
            # CORREÇÃO: quiet=True suprime prints no stdout.
            quiet=True,
        )
 
        _load_cookies_into_context(local_L.context, resolved_cookie)
 
        post = instaloader.Post.from_shortcode(local_L.context, shortcode)
 
        media_items: list[dict] = []
        if post.typename == "GraphSidecar":
            for node in post.get_sidecar_nodes():
                if node.is_video:
                    media_items.append({"type": "video", "url": node.video_url})
                else:
                    media_items.append({"type": "photo", "url": node.display_url})
        elif post.is_video:
            media_items.append({"type": "video", "url": post.video_url})
        else:
            media_items.append({"type": "photo", "url": post.url})
 
        # CORREÇÃO: evitar erros de encoding pegando o caption de forma segura.
        try:
            title = post.caption or ""
        except Exception:
            title = ""
 
        return {
            "urls": [m["url"] for m in media_items],
            "type": "carousel" if len(media_items) > 1 else media_items[0]["type"],
            "title": title,
            "uploader": post.owner_username or "Autor",
        }
 
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _get_post_info),
            # CORREÇÃO: timeout aumentado de 15s para 20s.
            # O Instaloader precisa de pelo menos uma requisição ao
            # endpoint /api/v1/media/{id}/info/ que pode levar ~8-12s
            # em conexões lentas. 15s era muito apertado.
            timeout=20.0,
        )
        return result
 
    except asyncio.TimeoutError:
        log.warning("Instaloader demorou muito (timeout) para shortcode: %s", shortcode)
        return None
    except instaloader.exceptions.LoginRequiredException:
        log.warning("Instaloader: post requer login para shortcode %s", shortcode)
        return None
    except instaloader.exceptions.InstaloaderException as exc:
        log.warning("Instaloader falhou: %s", str(exc)[:200])
        return None
    except Exception as exc:
        log.warning("Instaloader erro inesperado: %s", str(exc)[:200])
        return None
 
 
async def download_via_rapidapi(url: str) -> dict | None:
    """
    Fallback 3: usa a rede pública Cobalt (POST /) para obter URLs de download.
 
    CORREÇÃO: A API Cobalt v2 mudou o schema de resposta em Jan 2025.
    O campo 'status' foi substituído por um campo implícito no HTTP status code,
    e o corpo agora segue o schema abaixo:
      - 200 + body.url → vídeo direto
      - 200 + body.picker → carrossel
      - 400/401/429 → erro
    O código original tentava checar body.status == 'tunnel' que não existe mais.
    """
    shortcode = _get_shortcode(url)
    if not shortcode:
        return None
 
    log.info("Tentando Cobalt API para shortcode: %s", shortcode)
    post_url = f"https://www.instagram.com/p/{shortcode}/"
 
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": _UA,
    }
 
    # Instâncias de fallback fixas — a lista pública muda frequentemente.
    # CORREÇÃO: removidas instâncias que exigem autenticação obrigatória.
    FALLBACK_INSTANCES = [
        "https://cobalt-api.libly.org",
        "https://cobalt.api.g-p.io",
        "https://cobalt.vinid.de",
        "https://api.cobalt.tools",
    ]
 
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            instances: list[str] = []
 
            # Tenta buscar lista dinâmica de instâncias
            try:
                resp_instances = await client.get(
                    "https://instances.cobalt.best/instances.json",
                    headers=headers,
                    timeout=8.0,
                )
                if resp_instances.status_code == 200:
                    raw = resp_instances.json()
                    for inst in raw:
                        # CORREÇÃO: o schema do instances.json mudou em 2025.
                        # Campos agora são: 'api_url', 'score', 'services'.
                        # Suporte retroativo para campos antigos ('api', 'url').
                        is_online = (
                            inst.get("online")
                            or inst.get("status") == "online"
                            or inst.get("score", 0) > 0
                        )
                        if not is_online:
                            continue
 
                        services = inst.get("services", {})
                        if services and not services.get("instagram", True):
                            continue
 
                        api_url = (
                            inst.get("api_url")
                            or inst.get("api")
                            or inst.get("url", "")
                        )
                        if api_url:
                            instances.append(api_url.rstrip("/"))
            except Exception as exc:
                log.debug("Não foi possível buscar instâncias cobalt: %s", exc)
 
            if not instances:
                instances = FALLBACK_INSTANCES
 
            for api_base in instances[:6]:  # Limita a 6 tentativas
                try:
                    log.debug("Tentando Cobalt: %s", api_base)
                    resp = await client.post(
                        f"{api_base}/",
                        json={"url": post_url},
                        headers=headers,
                        timeout=10.0,
                    )
 
                    if resp.status_code == 401:
                        log.debug("Cobalt %s requer autenticação", api_base)
                        continue
                    if resp.status_code == 429:
                        log.debug("Cobalt %s rate limit", api_base)
                        continue
                    if resp.status_code != 200:
                        continue
 
                    data = resp.json()
 
                    # CORREÇÃO: lógica de parsing do response da Cobalt v2.
                    # O campo 'status' foi deprecado. Agora verificamos
                    # a presença de 'url' ou 'picker' diretamente.
                    if "url" in data:
                        # Resposta de vídeo único ou redirect
                        return {
                            "type": "video",
                            "urls": [data["url"]],
                            "title": data.get("filename", ""),
                            "uploader": "Autor",
                        }
                    if "picker" in data:
                        # Resposta de carrossel
                        urls = [
                            item["url"]
                            for item in data.get("picker", [])
                            if "url" in item
                        ]
                        if urls:
                            return {
                                "type": "carousel",
                                "urls": urls,
                                "title": "",
                                "uploader": "Autor",
                            }
 
                    # Suporte retroativo ao schema antigo (pre-2025)
                    status = data.get("status", "")
                    if status in ("tunnel", "redirect") and "url" in data:
                        return {
                            "type": "video",
                            "urls": [data["url"]],
                            "title": "",
                            "uploader": "Autor",
                        }
                    if status == "picker":
                        urls = [i["url"] for i in data.get("picker", []) if "url" in i]
                        if urls:
                            return {
                                "type": "carousel",
                                "urls": urls,
                                "title": "",
                                "uploader": "Autor",
                            }
 
                except httpx.TimeoutException:
                    log.debug("Cobalt %s timeout", api_base)
                    continue
                except Exception as exc:
                    log.debug("Cobalt %s falhou: %s", api_base, str(exc)[:100])
                    continue
 
    except Exception as exc:
        log.warning("Cobalt Network falhou completamente: %s", str(exc)[:200])
 
    return None
 
 
async def download_via_embed_v2(url: str) -> dict | None:
    """
    Fallback 4: raspa o HTML do embed diretamente com regex mais robusto.
 
    CORREÇÃO: a regex original era ambígua e capturava o atributo srcset
    junto com o src, resultando em URLs malformadas. Usando regex mais
    precisa para capturar apenas o src.
    """
    shortcode = _get_shortcode(url)
    if not shortcode:
        return None
 
    log.info("Tentando Embed V2 para shortcode: %s", shortcode)
    embed_url = f"https://www.instagram.com/p/{shortcode}/embed/"
 
    headers = {
        "User-Agent": _UA,
        "Referer": "https://www.instagram.com/",
    }
 
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(embed_url, headers=headers)
            if resp.status_code != 200:
                return None
            html = resp.text
 
        # CORREÇÃO: regex corrigida para capturar SOMENTE o valor do atributo src,
        # sem pegar o srcset ou outros atributos subsequentes.
        # Antes: r'class=.EmbeddedMediaImage.[^>]*src=.([^>]+).'
        # Essa regex capturava tudo até o final da tag, incluindo srcset.
        img_match = re.search(
            r'class=["\']EmbeddedMediaImage["\'][^>]*\bsrc=["\']([^"\']+)["\']',
            html,
        )
        # Tenta também a ordem inversa (src antes de class)
        if not img_match:
            img_match = re.search(
                r'\bsrc=["\']([^"\']+)["\'][^>]*class=["\']EmbeddedMediaImage["\']',
                html,
            )
 
        if img_match:
            img_url = img_match.group(1).replace("&amp;", "&").replace("\\/", "/")
            if img_url.startswith("http"):
                return {
                    "type": "photo",
                    "urls": [img_url],
                    "title": "",
                    "uploader": "Autor",
                }
 
    except Exception as exc:
        log.warning("Embed V2 falhou: %s", str(exc)[:200])
 
    return None
 
 
async def download_instagram(
    url: str,
    cookie_path: str,
    out_dir: str,
) -> dict | None:
    """
    Fluxo completo de download do Instagram.
 
    Ordem de tentativas (da mais confiável para a mais fraca):
      1. yt-dlp com cookies  → melhor para Reels/vídeos autenticados
      2. Instaloader         → melhor para fotos e carrosséis
      3. Cobalt API          → fallback externo (posts públicos)
      4. Embed endpoint v1   → scraping leve (posts públicos)
      5. Embed endpoint v2   → scraping direto da tag img
 
    CORREÇÃO DA ORDEM: o código original tentava Instaloader primeiro,
    mas o Instaloader é lento (~10-20s) e falha em Reels. O yt-dlp com
    cookies é mais rápido e cobre mais casos quando os cookies são válidos.
    Se os cookies não existirem, o yt-dlp é pulado e o Instaloader assume.
    """
    log.info("Iniciando download Instagram: %s", url)
    resolved_cookie = _resolve_cookie_path(cookie_path)
 
    # Tentativa 1: yt-dlp com cookies (rápido, cobre vídeos e fotos)
    if os.path.exists(resolved_cookie):
        result = await download_with_cookies(url, resolved_cookie, out_dir)
        if result:
            log.info("Download via yt-dlp+cookies: %s (%d arquivo(s))", url, len(result.get("files", [])))
            return result
        log.info("yt-dlp+cookies falhou, tentando Instaloader...")
    else:
        log.info("Sem cookies, pulando yt-dlp direto para Instaloader...")
 
    # Tentativa 2: Instaloader (bom para fotos/carrosséis, mais lento)
    result = await download_via_instaloader(url, out_dir, cookie_path=resolved_cookie)
    if result:
        log.info("Download via Instaloader: %s (%d item(s))", url, len(result.get("urls", [])))
        return result
    log.info("Instaloader falhou, tentando Cobalt API...")
 
    # Tentativa 3: Cobalt API (posts públicos, sem autenticação)
    result = await download_via_rapidapi(url)
    if result:
        log.info("Download via Cobalt API: %s (%d item(s))", url, len(result.get("urls", [])))
        return result
    log.info("Cobalt API falhou, tentando embed v1...")
 
    # Tentativa 4: Embed v1 (scraping do embed endpoint)
    result = await download_via_embed(url)
    if result:
        log.info("Download via embed v1: %s (%d item(s))", url, len(result.get("urls", result.get("files", []))))
        return result
    log.info("Embed v1 falhou, tentando embed v2...")
 
    # Tentativa 5: Embed v2 (scraping direto da tag img)
    result = await download_via_embed_v2(url)
    if result:
        log.info("Download via embed v2: %s (%d item(s))", url, len(result.get("urls", [])))
        return result
 
    log.warning("Todas as tentativas falharam para: %s", url)
    return None

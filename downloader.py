"""Downloader de vídeos baseado em yt-dlp.

Fluxo:
1. extract_info(download=False) consulta metadados sem baixar;
2. valida duração (bloqueio antes de gastar token do usuário);
3. escolhe o melhor formato <= 720p, preferindo MP4 + áudio compatível;
4. baixa para um diretório temporário e mescla com FFmpeg quando necessário.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yt_dlp

from config import Settings

# Bot API do Telegram: limite de 50 MB para envio via bot.
# Usamos 45 MB de folga para evitar 413 "Request Entity Too Large".
MAX_UPLOAD_BYTES = 45 * 1024 * 1024


class VideoTooLargeError(Exception):
    """Não foi possível reduzir o vídeo ao limite de envio do Telegram."""

from urllib.parse import urlparse


class VideoTooLongError(Exception):
    """Vídeo excede o limite de duração permitido."""


class DownloadError(Exception):
    """Falha ao extrair metadados ou baixar o vídeo."""


def normalize_url(text: str) -> str | None:
    """Extrai e normaliza uma URL de um texto qualquer.

    Aceita:
    - https://youtube.com/watch?v=...
    - youtube.com/watch?v=...   (sem scheme)
    - www.youtube.com/watch?v=...
    - texto com a URL no meio: "olha esse: https://youtu.be/abc"
    Retorna a URL com https:// ou None se não encontrar nada válido.
    """
    text = text.strip()
    # Se já é uma URL completa, pega só ela
    match = re.search(r"https?://[^\s<>\"']+", text)
    if match:
        candidate = match.group(0)
    else:
        # Qualquer domínio com ponto e caminho, mesmo sem scheme.
        match = re.search(
            r"(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/[^\s<>\"']*)?",
            text,
            re.IGNORECASE,
        )
        if not match:
            return None
        candidate = "https://" + match.group(0)

    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if "." not in parsed.netloc:
        return None
    return candidate


def is_valid_url(url: str) -> bool:
    """Valida se a string parece uma URL http(s) com domínio."""
    return normalize_url(url) is not None


@dataclass(frozen=True, slots=True)
class VideoInfo:
    """Metadados mínimos necessários para decisão de download."""

    title: str
    duration: int
    id: str
    webpage_url: str
    format_id: str | None = None


def _estimate_bytes(fmt: dict, duration: int) -> int:
    """Estima o tamanho de um formato usando filesize ou bitrate (tbr)."""
    filesize = fmt.get("filesize") or fmt.get("filesize_approx")
    if filesize:
        return int(filesize)
    tbr = fmt.get("tbr") or fmt.get("vbr") or 0
    if tbr and duration:
        # tbr está em kbps; bytes = kbps * 1000 / 8 * segundos
        return int(float(tbr) * 1000 / 8 * duration)
    return 0


def _pick_best_format(info: dict, duration: int, max_height: int) -> str | None:
    """Escolhe o formato que cabe no limite do Telegram com a melhor qualidade.

    Usa apenas metadados (já obtidos no extract_info), sem baixar nada.
    """
    formats = info.get("formats") or []
    candidates = []
    for f in formats:
        if f.get("vcodec") == "none":
            continue  # ignora formatos só de áudio
        height = f.get("height") or 0
        if height and height > max_height:
            continue
        est = _estimate_bytes(f, duration)
        # Sem estimativa de tamanho, não dá para garantir que cabe: deprioriza.
        if est == 0:
            continue
        if est > MAX_UPLOAD_BYTES:
            continue
        # Prefere maior resolução entre os que cabem.
        candidates.append((height or 0, est, str(f.get("format_id"))))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][2]


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Arquivo baixado e pronto para envio."""

    path: Path
    title: str


class VideoDownloader:
    """Extrai metadados e baixa vídeos respeitando os limites configurados."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ---------------------------------------------------------------- metadados
    def extract_info(self, url: str) -> VideoInfo:
        """Consulta metadados sem baixar nada (skip_download)."""
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 20,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # yt-dlp lança vários tipos de erro
            raise DownloadError(
                "Não consegui ler esse link. Verifique se ele é público e válido."
            ) from exc

        if not info:
            raise DownloadError("Não consegui obter informações desse vídeo.")

        duration = int(info.get("duration") or 0)
        if duration > self._settings.max_duration_seconds:
            minutes = duration / 60
            limit = self._settings.max_duration_seconds / 60
            raise VideoTooLongError(
                f"Esse vídeo tem {minutes:.1f} min, acima do limite de {limit:.0f} min. "
                "Escolha um vídeo mais curto, por favor."
            )

        format_id = _pick_best_format(info, duration, self._settings.max_height)

        return VideoInfo(
            title=str(info.get("title") or "vídeo"),
            duration=duration,
            id=str(info.get("id") or ""),
            webpage_url=str(info.get("webpage_url") or url),
            format_id=format_id,
        )

    # ----------------------------------------------------------------- download
    def _format_selector(self) -> str:
        """Seletor que prefere formatos menores para caber no limite do Telegram."""
        h = self._settings.max_height
        return (
            f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={h}]+bestaudio/"
            f"best[height<={h}][ext=mp4]/"
            f"best[height<={h}]/"
            f"bestvideo+bestaudio/best"
        )

    def _download_with_format(
        self, url: str, target_dir: Path, fmt: str, tag: str
    ) -> tuple[Path, dict]:
        """Baixa com um formato específico e retorna (caminho, info)."""
        outtmpl = str(target_dir / f"%(id)s_{tag}.%(ext)s")
        ffmpeg = self._settings.ffmpeg_path
        opts: dict = {
            "format": fmt,
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 20,
            "retries": 2,
            "merge_output_format": "mp4",
            "postprocessors": [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
            ],
        }
        if ffmpeg:
            opts["ffmpeg_location"] = ffmpeg

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

        path = Path(filename)
        if not path.exists():
            candidates = sorted(target_dir.glob(f"{info.get('id', '*')}_{tag}.*"))
            if not candidates:
                candidates = sorted(target_dir.glob(f"{info.get('id', '*')}.*"))
            if not candidates:
                raise DownloadError("O arquivo baixado não foi encontrado.")
            path = candidates[0]
        return path, info

    def download(
        self, url: str, target_dir: Path, preferred_format: str | None = None
    ) -> DownloadResult:
        """Baixa o vídeo em uma única passada quando possível.

        Se preferred_format foi calculado nos metadados, ele é usado direto
        (rápido). Fallbacks só entram se o arquivo ainda passar do limite.
        """
        info: dict = {}
        path: Path | None = None

        # Formato escolhido nos metadados = prioridade (uma única passada).
        if preferred_format:
            attempts = [(preferred_format, "pick")]
        else:
            attempts = []

        # Fallbacks: do maior para o menor, parando no primeiro que caiba.
        attempts += [
            (self._format_selector(), "best"),
            ("best[height<=480]/bestvideo[height<=480]+bestaudio", "480"),
            ("best[height<=360]/bestvideo[height<=360]+bestaudio", "360"),
            ("worst[height>=240]/worst", "worst"),
        ]
        for fmt, tag in attempts:
            try:
                path, info = self._download_with_format(url, target_dir, fmt, tag)
            except DownloadError:
                raise
            except Exception as exc:
                raise DownloadError(
                    "Falhei ao baixar este vídeo. Ele pode estar indisponível ou restrito."
                ) from exc
            if path.stat().st_size <= MAX_UPLOAD_BYTES:
                return DownloadResult(
                    path=path, title=str(info.get("title") or "vídeo")
                )
            # Arquivo grande: remove e tenta a próxima resolução.
            path.unlink(missing_ok=True)

        # Nenhuma resolução nativa coube; última tentativa: re-encode via FFmpeg.
        # Rebaixa o primeiro arquivo (melhor qualidade) para compressão.
        try:
            path, info = self._download_with_format(
                url, target_dir, self._format_selector(), "reencode"
            )
        except Exception as exc:
            raise DownloadError("Falhei ao preparar o vídeo para compressão.") from exc

        path = self._shrink_to_limit(path, target_dir, self._settings.ffmpeg_path)
        return DownloadResult(path=path, title=str(info.get("title") or "vídeo"))

    # --------------------------------------------------------------- compressão
    def _shrink_to_limit(
        self, path: Path, target_dir: Path, ffmpeg: str | None
    ) -> Path:
        """Re-encode com FFmpeg em bitrates decrescentes até caber no limite."""
        ffmpeg_bin = ffmpeg or "ffmpeg"
        for bitrate in ("2500k", "1800k", "1200k", "800k", "500k"):
            out = target_dir / f"{path.stem}_{bitrate}.mp4"
            cmd = [
                ffmpeg_bin,
                "-y",
                "-loglevel", "error",
                "-i", str(path),
                "-vf", f"scale='min({self._settings.max_height},iw)':-2",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-b:v", bitrate,
                "-maxrate", bitrate,
                "-bufsize", f"{int(bitrate[:-1]) * 2}k",
                "-c:a", "aac",
                "-b:a", "96k",
                "-movflags", "+faststart",
                str(out),
            ]
            try:
                result = subprocess.run(
                    cmd, check=True, capture_output=True, text=True, timeout=900
                )
            except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
                import logging

                logging.getLogger(__name__).warning(
                    "FFmpeg falhou no bitrate %s: %s", bitrate, exc
                )
                break
            if out.exists() and out.stat().st_size <= MAX_UPLOAD_BYTES:
                path.unlink(missing_ok=True)
                return out
            out.unlink(missing_ok=True)

        raise VideoTooLargeError(
            "O vídeo é grande demais para o Telegram (limite de 50 MB) e não "
            "consegui reduzi-lo o suficiente. Tente um vídeo menor."
        )

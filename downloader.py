"""Downloader de vídeos baseado em yt-dlp.

Fluxo:
1. extract_info(download=False) consulta metadados sem baixar;
2. valida duração (bloqueio antes de gastar token do usuário);
3. escolhe o melhor formato <= 720p, preferindo MP4 + áudio compatível;
4. baixa para um diretório temporário e mescla com FFmpeg quando necessário.
"""

from __future__ import annotations

import os
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


def is_valid_url(url: str) -> bool:
    """Valida se a string parece uma URL http(s) com domínio."""
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


@dataclass(frozen=True, slots=True)
class VideoInfo:
    """Metadados mínimos necessários para decisão de download."""

    title: str
    duration: int
    id: str
    webpage_url: str


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

        return VideoInfo(
            title=str(info.get("title") or "vídeo"),
            duration=duration,
            id=str(info.get("id") or ""),
            webpage_url=str(info.get("webpage_url") or url),
        )

    # ----------------------------------------------------------------- download
    def download(self, url: str, target_dir: Path) -> DownloadResult:
        """Baixa o vídeo em target_dir com resolução máxima configurada."""
        height = self._settings.max_height
        # prioriza mp4; se não houver, aceita qualquer container e converte
        max_size = str(MAX_UPLOAD_BYTES)
        format_selector = (
            f"bestvideo[height<={height}][filesize<{max_size}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={height}][filesize<{max_size}]+bestaudio/"
            f"best[height<={height}][filesize<{max_size}][ext=mp4]/"
            f"best[height<={height}][filesize<{max_size}]/"
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]/best"
        )

        outtmpl = str(target_dir / "%(id)s.%(ext)s")
        ffmpeg = self._settings.ffmpeg_path
        opts: dict = {
            "format": format_selector,
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 20,
            "retries": 2,
            "merge_output_format": "mp4",
            "postprocessors": [
                {
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "mp4",
                }
            ],
        }
        if ffmpeg:
            opts["ffmpeg_location"] = ffmpeg

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
        except Exception as exc:
            raise DownloadError(
                "Falhei ao baixar este vídeo. Ele pode estar indisponível ou restrito."
            ) from exc

        path = Path(filename)
        # O postprocessor pode trocar a extensão para .mp4
        if not path.exists():
            candidates = sorted(target_dir.glob(f"{info.get('id', '*')}.*"))
            if not candidates:
                raise DownloadError("O arquivo baixado não foi encontrado.")
            path = candidates[0]

        # Se ainda passar do limite, re-encode progressivamente até caber.
        if path.stat().st_size > MAX_UPLOAD_BYTES:
            path = self._shrink_to_limit(path, target_dir, ffmpeg)

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
                subprocess.run(cmd, check=True, capture_output=True, timeout=900)
            except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
                break
            if out.exists() and out.stat().st_size <= MAX_UPLOAD_BYTES:
                path.unlink(missing_ok=True)
                return out
            out.unlink(missing_ok=True)

        raise VideoTooLargeError(
            "O vídeo é grande demais para o Telegram (limite de 50 MB) e não "
            "consegui reduzi-lo o suficiente. Tente um vídeo menor."
        )

"""Configurações centralizadas do bot.

Todas as credenciais e parâmetros ajustáveis vêm de variáveis de ambiente,
carregadas a partir de um arquivo .env (python-dotenv).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Carrega .env do diretório do projeto (sem sobrescrever variáveis já definidas).
load_dotenv(Path(__file__).resolve().parent / ".env")


def _int_env(name: str, default: int) -> int:
    """Lê um inteiro do ambiente com fallback seguro."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} deve ser um número inteiro, recebeu: {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    """Configuração imutável da aplicação."""

    bot_token: str
    pix_provider: str
    pix_amount_cents: int
    pix_webhook_secret: str
    db_path: Path
    max_duration_seconds: int
    max_height: int
    cooldown_seconds: int
    free_tokens: int
    tokens_per_payment: int
    ffmpeg_path: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "BOT_TOKEN não definido. Copie .env.example para .env e preencha o token."
            )

        return cls(
            bot_token=token,
            pix_provider=os.getenv("PIX_PROVIDER", "mock").strip().lower(),
            pix_amount_cents=_int_env("PIX_AMOUNT_CENTS", 500),
            pix_webhook_secret=os.getenv("PIX_WEBHOOK_SECRET", "").strip(),
            db_path=Path(os.getenv("DB_PATH", "bot.db")).resolve(),
            max_duration_seconds=_int_env("MAX_DURATION_SECONDS", 1200),
            max_height=_int_env("MAX_HEIGHT", 720),
            cooldown_seconds=_int_env("COOLDOWN_SECONDS", 30),
            free_tokens=_int_env("FREE_TOKENS", 1),
            tokens_per_payment=_int_env("TOKENS_PER_PAYMENT", 1),
            ffmpeg_path=(
                os.getenv("FFMPEG_PATH")
                or (
                    "/opt/render/project/src/bin/ffmpeg"
                    if Path("/opt/render/project/src/bin/ffmpeg").exists()
                    else None
                )
            ),
        )


settings = Settings.from_env()

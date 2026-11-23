"""Ponto de entrada do bot Telegram.

Execução:
    python bot.py

Requer BOT_TOKEN no .env. Use /start, envie um link de vídeo e /pix para recarregar.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import settings
from db import Database
from health import start_health_server
from downloader import (
    DownloadError,
    VideoDownloader,
    VideoTooLargeError,
    VideoTooLongError,
    is_valid_url,
)
from pix_provider import APPROVED, get_pix_provider

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

db = Database(settings.db_path, settings)
downloader = VideoDownloader(settings)
pix = get_pix_provider(settings)


# ---------------------------------------------------------------------- helpers
async def _safe_reply(update: Update, text: str) -> None:
    """Envia mensagem de texto com Markdown, caindo para texto puro se necessário."""
    if update.effective_message is None:
        return
    try:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await update.effective_message.reply_text(text)


def _user_identity(update: Update) -> tuple[int, str | None, str | None]:
    user = update.effective_user
    if user is None:
        raise RuntimeError("Usuário não identificado.")
    return user.id, user.username, user.first_name


# ---------------------------------------------------------------------- comandos
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id, username, first_name = _user_identity(update)
    user = db.get_or_create_user(user_id, username, first_name)
    await _safe_reply(
        update,
        "Olá! 👋 Enviei um link de vídeo público e eu baixo para você (até 720p / 20 min).\n\n"
        f"Tokens disponíveis: *{user['tokens']}*\n"
        "Use /saldo para consultar e /pix para recarregar por R$ 5,00.",
    )


async def saldo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id, username, first_name = _user_identity(update)
    user = db.get_or_create_user(user_id, username, first_name)
    await _safe_reply(update, f"Seu saldo: *{user['tokens']}* token(s).")


async def pix_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id, username, first_name = _user_identity(update)
    db.get_or_create_user(user_id, username, first_name)

    charge = pix.create_charge(settings.pix_amount_cents, external_id=str(user_id))
    db.register_charge(
        user_id=user_id,
        provider=pix.name,
        charge_id=charge.charge_id,
        amount_cents=charge.amount_cents,
    )

    valor = charge.amount_cents / 100
    texto = (
        f"Pix de *R$ {valor:.2f}* gerado!\n\n"
        f"Código Copia e Cola:\n`{charge.pix_copy_paste}`\n\n"
        "Após o pagamento, seus tokens são liberados automaticamente "
        "assim que a confirmação oficial chegar."
    )
    if update.effective_message is not None:
        await update.effective_message.reply_text(texto, parse_mode=ParseMode.MARKDOWN)
        if charge.qr_code_base64:
            import base64

            await update.effective_message.reply_photo(
                photo=base64.b64decode(charge.qr_code_base64),
                caption="QR Code Pix",
            )

    # Verificação imediata (para providers que confirmam rápido) + polling curto.
    asyncio.create_task(_watch_charge(charge.charge_id, context))


async def _watch_charge(charge_id: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirma status oficial e credita; roda por até ~5 min em segundo plano."""
    for _ in range(20):
        await asyncio.sleep(15)
        status = pix.get_charge_status(charge_id)
        if status == APPROVED and db.mark_approved_and_credit(pix.name, charge_id):
            # Recupera o usuário pelo charge registrado
            row = db._connection().execute(
                "SELECT user_id FROM payments WHERE provider = ? AND charge_id = ?",
                (pix.name, charge_id),
            ).fetchone()
            if row:
                try:
                    await context.bot.send_message(
                        chat_id=row["user_id"],
                        text=f"✅ Pagamento confirmado! {settings.tokens_per_payment} token(s) creditado(s).",
                    )
                except Exception:
                    logger.warning("Não foi possível avisar o usuário do crédito.")
            return
        elif status and status != "pending":
            db.mark_status(pix.name, charge_id, status)


# --------------------------------------------------------------------- downlaod
async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    if not is_valid_url(text):
        await _safe_reply(update, "Isso não parece um link válido. Envie uma URL de vídeo pública.")
        return

    user_id, username, first_name = _user_identity(update)
    user = db.get_or_create_user(user_id, username, first_name)

    if int(user["tokens"]) < 1:
        await _safe_reply(
            update,
            "Você não tem tokens. Use /pix para recarregar por R$ 5,00.",
        )
        return

    restante = db.cooldown_remaining(user_id)
    if restante > 0:
        await _safe_reply(
            update,
            f"⏳ Aguarde {restante:.0f}s antes de enviar outro link (anti-spam).",
        )
        return

    db.touch_request(user_id)

    # 1) metadados primeiro — bloqueio sem gasto de token
    try:
        info = await asyncio.to_thread(downloader.extract_info, text)
    except VideoTooLongError as exc:
        await _safe_reply(update, str(exc))
        return
    except DownloadError as exc:
        await _safe_reply(update, str(exc))
        return

    # 2) download em diretório temporário com limpeza automática
    if update.effective_message is not None:
        await update.effective_chat.send_action(ChatAction.UPLOAD_VIDEO)

    with tempfile.TemporaryDirectory(prefix="telegram_bot_") as tmp:
        try:
            result = await asyncio.to_thread(
                downloader.download, text, Path(tmp)
            )
        except (DownloadError, VideoTooLargeError) as exc:
            await _safe_reply(update, str(exc))
            return

        # 3) envia e só então debita o token
        try:
            if update.effective_message is not None:
                with result.path.open("rb") as video:
                    await update.effective_message.reply_video(
                        video=video,
                        caption=result.title[:100],
                        supports_streaming=True,
                    )
        except Exception as exc:
            logger.exception("Falha ao enviar vídeo")
            await _safe_reply(
                update,
                "O download funcionou, mas falhei ao enviar. Nenhum token foi cobrado. "
                "Tente novamente em instantes.",
            )
            return

        if db.consume_token(user_id):
            saldo = db.get_tokens(user_id)
            await _safe_reply(update, f"✅ Pronto! Token cobrado. Saldo: *{saldo}*.")
        else:
            await _safe_reply(update, "✅ Pronto!")

    logger.info("Vídeo entregue: %s para user %s (%ss)", info.id, user_id, info.duration)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Erro no bot: %s", context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        await update.effective_message.reply_text(
            "Ocorreu um erro inesperado. Já registrei o problema e vou melhorar. "
            "Tente novamente em instantes."
        )


def main() -> None:
    application: Application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .concurrent_updates(True)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("saldo", saldo))
    application.add_handler(CommandHandler("pix", pix_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)
    )
    application.add_error_handler(error_handler)

    # Em Web Services (Render Free), PORT é definido pelo provedor.
    # Subimos um health server para manter o processo classificado como web.
    port = os.getenv("PORT")
    if port:
        start_health_server(port=int(port))
        logger.info("Health server na porta %s", port)

    logger.info("Bot iniciado. DB=%s Pix=%s", settings.db_path, pix.name)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

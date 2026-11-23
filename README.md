# Bot de vídeos para Telegram

Bot público que baixa vídeos de links, com limite de 20 min / 720p, cooldown por usuário e sistema de tokens recarregáveis via Pix.

## Rodando localmente (notebook / terminal)

```bash
# 1. crie e ative um ambiente virtual
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

# 2. instale as dependências
pip install -r requirements.txt

# 3. configure
copy .env.example .env   # Windows
# cp .env.example .env   # Linux/Mac
# edite .env e cole seu BOT_TOKEN

# 4. FFmpeg (necessário para mesclar áudio/vídeo e converter para MP4)
# Baixe em https://ffmpeg.org/download.html e coloque no PATH,
# ou defina FFMPEG_PATH no .env

# 5. rode
python bot.py
```

No notebook (Colab/Jupyter), use `!pip install -r requirements.txt` e depois `!python bot.py`. O bot usa long polling, então não precisa de webhook público para testar.

## Comandos

- `/start` — cria o usuário e dá 1 token grátis
- envie um link de vídeo público — baixa e envia o vídeo
- `/saldo` — consulta tokens
- `/pix` — gera cobrança de R$ 5,00 com Copia e Cola (+ QR Code se o provedor fornecer)

## Regras implementadas

- Metadados consultados antes do download: vídeos > 20 min são bloqueados sem cobrar token.
- Resolução máxima 720p, priorizando MP4 + áudio compatível, com merge/conversão via FFmpeg.
- Cooldown configurável por usuário (`COOLDOWN_SECONDS`).
- Download em `tempfile.TemporaryDirectory` com limpeza automática.
- Token debitado **somente após** envio bem-sucedido ao Telegram.
- SQLite com `UNIQUE(provider, charge_id)` e flag `credited`: um pagamento nunca credita duas vezes.
- Tokens liberados apenas após status oficial aprovado no provedor (nunca por comprovante do usuário).

## Conectando seu provedor Pix real

1. Abra `pix_provider.py`.
2. Crie uma classe que herde de `PixProvider` e implemente:
   - `create_charge(amount_cents, external_id)` → retorna `Charge` com `pix_copy_paste` e, se disponível, `qr_code_base64`.
   - `get_charge_status(charge_id)` → retorna `"approved"` quando a API confirmar.
   - Opcionalmente `verify_webhook` / `parse_webhook` para seu formato de assinatura.
3. Registre a classe em `get_pix_provider()` e ajuste `PIX_PROVIDER` no `.env`.

O provider `mock` serve só para desenvolvimento: ele gera um código de demonstração e nunca aprova sozinho. Para testar o crédito localmente, chame `pix.approve_manually(charge_id)`; o polling em `_watch_charge` então credita os tokens.

## Estrutura

```
config.py        # configurações via .env
db.py            # SQLite: usuários, tokens, pagamentos, idempotência
downloader.py    # yt-dlp: metadados, validação e download
pix_provider.py  # interface + provider mock (isolado)
bot.py           # handlers Telegram e ponto de entrada
```

## Rodando 24/7 (hospedagem)

O bot usa **long polling**, então não precisa de webhook, IP fixo nem domínio. Basta um processo sempre ativo.

### Opção A — Render (Blueprint, recomendado para começar)

1. Crie um repositório no GitHub e envie estes arquivos (o `.env` **não** vai, ele está no `.gitignore`).
2. No [Render Dashboard](https://dashboard.render.com): **New → Blueprint** → conecte o repo.
3. Ele lê o `render.yaml` e cria um **Background Worker**.
4. Em `Environment`, preencha `BOT_TOKEN` com o token do @BotFather.
5. O `build.sh` instala as dependências e um FFmpeg estático automaticamente.
6. Pronto: o worker fica online 24/7 (plano free pode hibernar após inatividade; use um plano pago para evitar isso).

> O `render.yaml` já provisiona um **Postgres gerenciado** e injeta `DATABASE_URL` no worker. Com isso, saldos, tokens e pagamentos persistem mesmo se o worker reiniciar. Localmente, sem `DATABASE_URL`, o bot continua usando SQLite (`DB_PATH`).

### Opção B — Docker (Railway / Fly.io / VPS)

```bash
docker build -t buscalink-bot .
docker run -d --name buscalink \
  -e BOT_TOKEN=seu_token_aqui \
  -v bot_data:/app/data \
  --restart unless-stopped \
  buscalink-bot
```

O volume `bot_data` preserva o `bot.db` entre reinícios.

### Opção C — VPS com systemd

Crie `/etc/systemd/system/buscalink.service`:

```ini
[Unit]
Description=BuscaLink Telegram Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/caminho/do/projeto
ExecStart=/caminho/do/projeto/.venv/bin/python bot.py
Restart=always
RestartSec=5
EnvironmentFile=/caminho/do/projeto/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now buscalink
sudo systemctl status buscalink
```

## Aviso

Ajuste os termos de uso, limites e a integração de pagamentos conforme a legislação e as regras da plataforma antes de publicar. Revogue o token usado neste teste com `/revoke` no @BotFather antes de colar em qualquer serviço de hospedagem.

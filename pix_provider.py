"""Camada de provedores Pix (interface + implementações).

O bot depende apenas da interface abstrata ``PixProvider``. Para conectar um
provedor real (Mercado Pago, PSP, etc.), implemente ``create_charge``,
``get_charge_status`` e registre a instância em ``get_pix_provider``.

NUNCA confie em comprovante enviado pelo usuário: a liberação de tokens usa
exclusivamente ``get_charge_status`` / webhook assinado do provedor.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from config import Settings


APPROVED = "approved"


@dataclass(frozen=True, slots=True)
class Charge:
    """Cobrança Pix criada no provedor."""

    charge_id: str
    amount_cents: int
    pix_copy_paste: str
    qr_code_base64: str | None = None
    expires_at: str | None = None


class PixProvider(ABC):
    """Interface que todo provedor Pix deve implementar."""

    name: str

    @abstractmethod
    def create_charge(self, amount_cents: int, external_id: str) -> Charge:
        """Cria uma cobrança e retorna código Copia e Cola (+ QR Code se houver)."""

    @abstractmethod
    def get_charge_status(self, charge_id: str) -> str:
        """Consulta o status oficial da cobrança na API do provedor."""

    def verify_webhook(self, payload: bytes, signature: str, secret: str) -> bool:
        """Valida assinatura do webhook (HMAC-SHA256). Override se o provedor usar outro esquema."""
        if not secret:
            return False
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def parse_webhook(self, payload: bytes) -> tuple[str, str] | None:
        """Extrai (charge_id, status) do corpo do webhook. Ajuste por provedor."""
        try:
            data: Any = json.loads(payload)
        except (ValueError, TypeError):
            return None
        charge_id = data.get("id") or data.get("charge_id") or data.get("txid")
        status = data.get("status")
        if not charge_id or not status:
            return None
        return str(charge_id), str(status)


class MockPixProvider(PixProvider):
    """Provedor fictício para desenvolvimento local.

    Gera um código Copia e Cola de demonstração. Nunca aprova pagamentos sozinho:
    use ``approve_manually`` no console para simular a confirmação oficial.
    """

    name = "mock"

    def __init__(self) -> None:
        self._charges: dict[str, str] = {}

    def create_charge(self, amount_cents: int, external_id: str) -> Charge:
        charge_id = f"mock-{external_id}"
        self._charges[charge_id] = "pending"
        payload = (
            "00020126580014BR.GOV.BCB.PIX0136"
            f"demonstracao-{external_id}"
            f"52040000530398654{amount_cents / 100:05.2f}"
            "5802BR5913BOT DEMO6009SAO PAULO62070503***6304ABCD"
        )
        return Charge(
            charge_id=charge_id,
            amount_cents=amount_cents,
            pix_copy_paste=payload,
            qr_code_base64=None,
        )

    def get_charge_status(self, charge_id: str) -> str:
        return self._charges.get(charge_id, "pending")

    def approve_manually(self, charge_id: str) -> None:
        """Só para testes: simula a confirmação oficial da API."""
        self._charges[charge_id] = APPROVED


# --------------------------------------------------------------------- factory
def get_pix_provider(settings: Settings) -> PixProvider:
    """Fábrica de provedores. Adicione novos casos conforme necessário."""
    if settings.pix_provider == "mock":
        return MockPixProvider()
    raise RuntimeError(
        f"Provedor Pix {settings.pix_provider!r} não implementado. "
        "Adicione a classe em pix_provider.py e registre aqui."
    )

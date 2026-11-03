import os
import sys
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789")
os.environ.setdefault("CRAWL4AI_TOKEN", "test-token")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test-voice-skill.db")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("MIN_URLS", "3")
os.environ.setdefault("WP_TOKEN_SECRET", "test-wp-token-secret-0123456789")
os.environ.setdefault("CREAPULSE_CREDITS_SECRET_VOIX", "test-credits-secret-0123456789abcdef")
os.environ.setdefault("WP_BASE_URL", "https://staging.creapulse.fr")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

RECHARGE_URL = "https://staging.creapulse.fr/credits/"


class FakeWallet:
    """Porte-monnaie en mémoire, au contrat 079 : remplace l'appel HTTP de `app.wallet.call`."""

    def __init__(self, balance: int = 50, price: int = 10):
        self.balance, self.price = balance, price
        self.calls: list[tuple[str, dict]] = []
        self.failures: dict[str, list] = {}   # route → erreurs à lever, dans l'ordre
        self.debited: set[str] = set()

    def fail(self, route: str, *errors) -> None:
        self.failures.setdefault(route, []).extend(errors)

    def routes(self) -> list[str]:
        return [route for route, _ in self.calls]

    async def call(self, client, route, body):
        from app.wallet import WalletError
        self.calls.append((route, body))
        if self.failures.get(route):
            raise self.failures[route].pop(0)
        if route == "balance":
            return {"balance": self.balance, "paid": 0, "costs": {"generation": self.price},
                    "can_start": {"generation": self.balance >= self.price}, "recharge_url": RECHARGE_URL,
                    "closed_reason": None}
        if route == "debit":
            if body["usage_ref"] in self.debited:
                return {"debited": self.price, "balance": self.balance, "replayed": True}
            if self.balance < self.price:
                raise WalletError(402, "credits_insufficient",
                                  {"balance": self.balance, "required": self.price, "recharge_url": RECHARGE_URL})
            self.balance -= self.price
            self.debited.add(body["usage_ref"])
            return {"debited": self.price, "balance": self.balance, "replayed": False}
        if route == "refund":
            if body["usage_ref"] in self.debited:
                self.debited.discard(body["usage_ref"])
                self.balance += self.price
            return {"refunded": True, "replayed": False}
        if route == "cost":
            return {"state": "recorded"}
        raise AssertionError(route)


def login(client, email: str = "a@b.fr", user_id: int = 7, levels: tuple[int, ...] = ()) -> None:
    """Ouvre une session membre, comme le ferait le jeton WordPress du shortcode."""
    from app import gating
    client.cookies.set(gating.SESSION_COOKIE, gating.sign_session(email, user_id, levels))


@pytest.fixture(autouse=True)
def fresh_store():
    """Chaque test repart d'un store SQLite vide."""
    from app import store as store_mod
    path = "./test-voice-skill.db"
    if store_mod._store is not None:
        store_mod._store.close()
        store_mod._store = None
    for suffix in ("", "-journal"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass
    yield
    if store_mod._store is not None:
        store_mod._store.close()
        store_mod._store = None


@pytest.fixture(autouse=True)
def fake_wallet(monkeypatch):
    """Aucun test n'appelle WordPress : le porte-monnaie est simulé, 50 crédits, 10 par génération."""
    from app import wallet
    fake = FakeWallet()
    monkeypatch.setattr(wallet, "call", fake.call)
    return fake

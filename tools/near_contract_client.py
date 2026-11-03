"""Клиент смарт-контракта nexus_escrow (NEAR Testnet) — stdlib only.

Все вызовы идут строго в задеплоенный контракт через tools/near_rpc.py
(pure-Python ed25519 + Borsh). Прямых transfer'ов здесь нет: деньги
двигаются только методами эскроу.

Ключи:
  * менеджер — NEAR_PRIVATE_KEY (если задан реальный, не плейсхолдер);
  * агент   — NEAR_AGENT_PRIVATE_KEY / NEAR_AGENT_KEYS_JSON;
  * локальный фолбэк для разработки — ~/.near-credentials/testnet/<acc>.json
    (никогда не попадает в репозиторий и не печатается в лог).

Контракт: NEAR_ESCROW_CONTRACT (default nexus-escrow.yksunder.testnet).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

try:
    from near_rpc import (
        EXPLORER_TX, NearTestnetClient, ContractCallError, YOCTO_PER_NEAR,
        b58encode, ed25519_publickey, ed25519_sign, parse_near_private_key,
        yocto_to_near,
    )
    from research_agent_clients import env
except ImportError:  # пакетный контекст
    from tools.near_rpc import (
        EXPLORER_TX, NearTestnetClient, ContractCallError, YOCTO_PER_NEAR,
        b58encode, ed25519_publickey, ed25519_sign, parse_near_private_key,
        yocto_to_near,
    )
    from tools.research_agent_clients import env

DEFAULT_CONTRACT = "nexus-escrow.yksunder.testnet"
DEFAULT_AGENT_ACCOUNT = "research-bot.testnet"
MIN_STAKE_NEAR = 1.0
GAS_TGAS = 50

_PLACEHOLDERS = {
    "", "your_private_key_here", "ed25519:your_private_key_here",
    "0123456789abcdef", "<private-key>",
}


def near_to_yocto(amount_near: float) -> int:
    return int(round(float(amount_near) * YOCTO_PER_NEAR))


def _usable_key(key: str | None) -> bool:
    return bool(key) and key.strip() not in _PLACEHOLDERS


def _credential_file(account_id: str, credentials_dir: str | None = None) -> Path:
    root = (credentials_dir or env("NEAR_CREDENTIALS_DIR")
            or os.path.expanduser("~/.near-credentials/testnet"))
    return Path(root).expanduser() / f"{account_id}.json"


def _key_from_credentials(account_id: str,
                          credentials_dir: str | None = None) -> str:
    path = _credential_file(account_id, credentials_dir)
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(data.get("private_key") or "")


def resolve_private_key(account_id: str, role: str = "manager",
                        credentials_dir: str | None = None) -> str:
    """Ключ аккаунта: env → JSON-пул агента → локальные near-credentials.

    Плейсхолдеры (your_private_key_here) считаются отсутствием ключа —
    иначе демо пыталось бы подписывать мусором.
    """
    if role == "agent":
        direct = env("NEAR_AGENT_PRIVATE_KEY")
        if _usable_key(direct):
            return direct
        pool_raw = env("NEAR_AGENT_KEYS_JSON")
        if pool_raw:
            try:
                pool = json.loads(pool_raw)
            except json.JSONDecodeError:
                pool = {}
            if isinstance(pool, dict) and _usable_key(pool.get(account_id)):
                return pool[account_id]
    else:
        direct = env("NEAR_PRIVATE_KEY")
        if _usable_key(direct):
            return direct
    local = _key_from_credentials(account_id, credentials_dir)
    if _usable_key(local):
        return local
    raise RuntimeError(
        f"нет приватного ключа для {account_id} ({role}); задай "
        "NEAR_PRIVATE_KEY/NEAR_AGENT_PRIVATE_KEY или положи "
        f"~/.near-credentials/testnet/{account_id}.json")


def corrupt_seed(seed32: bytes) -> bytes:
    """Инверсия последнего байта seed — «сломанный» ключ фрилансера."""
    if not seed32:
        raise ValueError("пустой seed")
    return seed32[:-1] + bytes([seed32[-1] ^ 0xFF])


def bid_message(task_id: str, agent_id: str, price_yocto: int) -> bytes:
    return f"bid:{task_id}:{agent_id}:{price_yocto}".encode()


def sign_bid(private_key: str, task_id: str, agent_id: str,
             price_yocto: int) -> dict:
    """Валидная подпись bid: signature b58 + public key b58."""
    seed32, pubkey32 = parse_near_private_key(private_key)
    msg = bid_message(task_id, agent_id, price_yocto)
    sig = ed25519_sign(msg, seed32, pubkey32)
    return {"signature": b58encode(sig),
            "public_key": "ed25519:" + b58encode(pubkey32)}


def sign_bid_broken(private_key: str, task_id: str, agent_id: str,
                    price_yocto: int) -> dict:
    """Подпись сломанным ключом: seed инвертирован, pubkey ему не пара.

    Контракт проверяет подпись по ключу из реестра и обязан её отвергнуть.
    """
    seed32, _registered_pub = parse_near_private_key(private_key)
    broken = corrupt_seed(seed32)
    broken_pub = ed25519_publickey(broken)
    msg = bid_message(task_id, agent_id, price_yocto)
    sig = ed25519_sign(msg, broken, broken_pub)
    return {"signature": b58encode(sig),
            "public_key": "ed25519:" + b58encode(broken_pub)}


class NearContractClient:
    """Тонкая обёртка над методами nexus_escrow + реестром агентов."""

    def __init__(self, contract_id: str | None = None,
                 rpc: "NearTestnetClient | None" = None,
                 manager_account: str | None = None,
                 agent_account: str | None = None,
                 credentials_dir: str | None = None):
        self.contract_id = (contract_id or env("NEAR_ESCROW_CONTRACT")
                            or DEFAULT_CONTRACT)
        self.rpc = rpc or NearTestnetClient()
        self.manager_account = manager_account or env("NEAR_ACCOUNT_ID")
        self.agent_account = (agent_account or env("NEAR_AGENT_ACCOUNT")
                              or env("NEAR_RECEIVER_ID")
                              or DEFAULT_AGENT_ACCOUNT)
        self.credentials_dir = credentials_dir
        self._manager_key: str | None = None
        self._agent_key: str | None = None

    # ------------------------------------------------------------ keys
    @property
    def manager_key(self) -> str:
        if self._manager_key is None:
            self._manager_key = resolve_private_key(
                self.manager_account, "manager", self.credentials_dir)
        return self._manager_key

    @property
    def agent_key(self) -> str:
        if self._agent_key is None:
            self._agent_key = resolve_private_key(
                self.agent_account, "agent", self.credentials_dir)
        return self._agent_key

    # --------------------------------------------------------- preflight
    def preflight(self) -> dict:
        """Контракт должен быть задеплоен; иначе честная ошибка, без fallback."""
        if not self.manager_account:
            raise RuntimeError("NEAR_ACCOUNT_ID не задан — менеджер неизвестен")
        if not self.rpc.contract_deployed(self.contract_id):
            raise RuntimeError(
                f"контракт {self.contract_id} не задеплоен "
                "(code_hash пуст) — fallback на transfer удалён")
        self.manager_key  # валидируем наличие ключа до первой транзакции
        return {"contract": self.contract_id,
                "manager": self.manager_account,
                "agent": self.agent_account,
                "contract_balance_near": yocto_to_near(
                    self.rpc.account_balance_yocto(self.contract_id))}

    # ------------------------------------------------------------ calls
    def _call(self, step: str, method: str, args: dict, signer: str,
              key: str, deposit_yocto: int = 0,
              allow_failure: bool = False) -> dict:
        receipt = {
            "step": step,
            "method": method,
            "contract": self.contract_id,
            "signer": signer,
            "deposit_near": yocto_to_near(deposit_yocto),
            "status": "pending",
        }
        try:
            res = self.rpc.send_function_call(
                signer, self.contract_id, method, args, key,
                gas=GAS_TGAS * 10**12, deposit_yocto=deposit_yocto)
        except ContractCallError as e:
            receipt.update(status="failed", tx_hash=e.tx_hash,
                           explorer=EXPLORER_TX + e.tx_hash if e.tx_hash else "",
                           error=e.reason, logs=e.logs)
            if allow_failure:
                return receipt
            raise
        receipt.update(
            status="success", tx_hash=res["tx_hash"],
            explorer=EXPLORER_TX + res["tx_hash"], logs=res.get("logs", []))
        return receipt

    def get_agent(self, account_id: str) -> object:
        return self.rpc.view_function(
            self.contract_id, "get_agent", {"agent_id": account_id})

    def get_task(self, task_id: str) -> object:
        return self.rpc.view_function(
            self.contract_id, "get_task", {"task_id": task_id})

    def get_bids(self, task_id: str) -> object:
        return self.rpc.view_function(
            self.contract_id, "get_bids", {"task_id": task_id})

    def get_burned(self) -> object:
        return self.rpc.view_function(self.contract_id, "get_burned", {})

    def get_escrow_balance(self) -> int:
        """v2: сколько NEAR сейчас заблокировано в живых эскроу (yocto)."""
        value = self.rpc.view_function(
            self.contract_id, "get_escrow_balance", {})
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def get_burned_yocto(self) -> int:
        try:
            return int(self.get_burned() or 0)
        except (TypeError, ValueError):
            return 0

    def ensure_agent(self, account_id: str | None = None,
                     public_key: str | None = None,
                     stake_near: float = MIN_STAKE_NEAR) -> dict:
        """Регистрирует агента, если он ещё не в реестре (стейк ≥ 1 NEAR)."""
        account_id = account_id or self.agent_account
        info = self.get_agent(account_id)
        # near-sdk возвращает Option<(String,String,bool)> как JSON-объект
        # (public_key/stake/active) или массив — поддерживаем оба вида.
        # View отдаёт NearToken через f64-округление (1e24 → ...83222784),
        # поэтому сравниваем с допуском, а не с точным 10**24.
        min_stake_yocto = int(MIN_STAKE_NEAR * YOCTO_PER_NEAR * 0.999)
        if isinstance(info, dict):
            stake_yocto = int(info.get("stake", 0))
            if bool(info.get("active")) and stake_yocto >= min_stake_yocto:
                return {"step": "register_agent", "status": "already_registered",
                        "account": account_id,
                        "public_key": info.get("public_key"),
                        "stake_near": yocto_to_near(stake_yocto)}
        elif isinstance(info, list) and len(info) >= 3:
            stake_yocto = int(info[1])
            if bool(info[2]) and stake_yocto >= min_stake_yocto:
                return {"step": "register_agent", "status": "already_registered",
                        "account": account_id, "public_key": info[0],
                        "stake_near": yocto_to_near(stake_yocto)}
        key = self.agent_key if account_id == self.agent_account else \
            resolve_private_key(account_id, "agent", self.credentials_dir)
        _seed, pubkey32 = parse_near_private_key(key)
        pk = public_key or ("ed25519:" + b58encode(pubkey32))
        return self._call(
            "register_agent", "register_agent", {"public_key": pk},
            account_id, key, deposit_yocto=near_to_yocto(stake_near))

    def create_task(self, task_id: str, budget_near: float,
                    deadline_ms: int) -> dict:
        return self._call(
            "create_task", "create_task",
            {"task_id": task_id, "deadline_ms": int(deadline_ms)},
            self.manager_account, self.manager_key,
            deposit_yocto=near_to_yocto(budget_near))

    def submit_bid(self, task_id: str, price_yocto: int, signature: str,
                   account_id: str | None = None, key: str | None = None,
                   allow_failure: bool = False) -> dict:
        account_id = account_id or self.agent_account
        key = key or (self.agent_key if account_id == self.agent_account
                      else resolve_private_key(
                          account_id, "agent", self.credentials_dir))
        return self._call(
            "submit_bid", "submit_bid",
            {"task_id": task_id, "price_yocto": str(int(price_yocto)),
             "signature": signature},
            account_id, key, allow_failure=allow_failure)

    def accept_bid(self, task_id: str, agent_id: str) -> dict:
        return self._call(
            "accept_bid", "accept_bid",
            {"task_id": task_id, "agent_id": agent_id},
            self.manager_account, self.manager_key)

    def submit_work(self, task_id: str, work_hash: str,
                    account_id: str | None = None,
                    key: str | None = None) -> dict:
        account_id = account_id or self.agent_account
        key = key or (self.agent_key if account_id == self.agent_account
                      else resolve_private_key(
                          account_id, "agent", self.credentials_dir))
        return self._call(
            "submit_work", "submit_work",
            {"task_id": task_id, "work_hash": work_hash},
            account_id, key)

    def verify_and_release(self, task_id: str) -> dict:
        return self._call(
            "verify_and_release", "verify_and_release",
            {"task_id": task_id},
            self.manager_account, self.manager_key)

    def balance_near(self, account_id: str) -> float:
        return yocto_to_near(self.rpc.account_balance_yocto(account_id))

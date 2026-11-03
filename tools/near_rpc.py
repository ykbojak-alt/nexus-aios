"""NEAR Testnet JSON-RPC клиент — stdlib only (urllib).

Чтение (view_account / баланс) работает без ключей. Подпись транзакции —
встроенная: pure-Python ed25519 по RFC 8032 + Borsh-сериализация
фиксированной структуры Transfer + broadcast_tx_commit. Внешних
зависимостей нет: near-api-py/py-near тянут пакеты с C-сборкой и на
свежих Python (3.15) падают на этапе install — здесь ничего ставить
не нужно. Подпись не constant-time — для одной demo-транзакции на
testnet это приемлемо, production-кошелькам нужен libsodium.

Ключи и id аккаунтов читаются через research_agent_clients.env() —
т.е. из .env / os.environ / настроек AI-2, в коде секретов нет.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
import urllib.error
import urllib.request

try:
    from research_agent_clients import env
except ImportError:
    from tools.research_agent_clients import env  # пакетный контекст

DEFAULT_RPC = "https://test.rpc.fastnear.com"
YOCTO_PER_NEAR = 10**24
EXPLORER_TX = "https://explorer.testnet.near.org/transactions/"


def yocto_to_near(yocto: int) -> float:
    return yocto / YOCTO_PER_NEAR


# --------------------------------------------------------------- base58

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(data: bytes) -> str:
    """Base58 (bitcoin-алфавит) — формат хэшей и ключей NEAR."""
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58_ALPHABET[r] + out
    # ведущие нулевые байты → '1'
    pad = 0
    for b in data:
        if b == 0:
            pad += 1
        else:
            break
    return "1" * pad + (out or ("1" if pad == 0 else ""))


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        i = _B58_ALPHABET.find(ch)
        if i < 0:
            raise ValueError(f"base58: недопустимый символ {ch!r}")
        n = n * 58 + i
    pad = len(s) - len(s.lstrip("1"))
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * pad + body


# ------------------------------------------------------ ed25519 (RFC 8032)
# Reference-алгоритм: скалярное умножение на кривой Edwards25519.
# Не constant-time — только для demo-транзакций в testnet.

_Q = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493


def _q_inv(x: int) -> int:
    return pow(x, _Q - 2, _Q)


_D = (-121665 * _q_inv(121666)) % _Q
_I_SQRT_M1 = pow(2, (_Q - 1) // 4, _Q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _q_inv(_D * y * y + 1) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q:
        x = x * _I_SQRT_M1 % _Q
    if x & 1:
        x = _Q - x
    return x


_BY = 4 * _q_inv(5) % _Q
_BX = _xrecover(_BY)
_B = (_BX, _BY, 1, _BX * _BY % _Q)


def _ed_add(p: tuple, q: tuple) -> tuple:
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % _Q
    b = (y1 + x1) * (y2 + x2) % _Q
    c = 2 * t1 * t2 * _D % _Q
    dd = 2 * z1 * z2 % _Q
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f % _Q, g * h % _Q, f * g % _Q, e * h % _Q)


def _scalarmult(p: tuple, e: int) -> tuple:
    if e == 0:
        return (0, 1, 1, 0)
    q = _scalarmult(p, e // 2)
    q = _ed_add(q, q)
    return _ed_add(q, p) if e & 1 else q


def _encodepoint(p: tuple) -> bytes:
    x, y, z, _t = p
    zi = _q_inv(z)
    x, y = x * zi % _Q, y * zi % _Q
    v = y | ((x & 1) << 255)
    return v.to_bytes(32, "little")


def _bit(h: bytes, i: int) -> int:
    return (h[i // 8] >> (i % 8)) & 1


def _secret_scalar(seed32: bytes) -> tuple[int, bytes]:
    """(a, h): приватный скаляр и полный sha512(seed) — нужен h[32:] для nonce."""
    h = hashlib.sha512(seed32).digest()
    a = 2**254 + sum(2**i * _bit(h, i) for i in range(3, 254))
    return a, h


def ed25519_publickey(seed32: bytes) -> bytes:
    a, _h = _secret_scalar(seed32)
    return _encodepoint(_scalarmult(_B, a))


def ed25519_sign(message: bytes, seed32: bytes,
                 publickey: bytes | None = None) -> bytes:
    """Ed25519-подпись RFC 8032: R + S (64 байта)."""
    a, h = _secret_scalar(seed32)
    pk = publickey or ed25519_publickey(seed32)
    r = int.from_bytes(hashlib.sha512(h[32:64] + message).digest(), "little") % _L
    r_enc = _encodepoint(_scalarmult(_B, r))
    s = (r + int.from_bytes(
        hashlib.sha512(r_enc + pk + message).digest(), "little") * a) % _L
    return r_enc + s.to_bytes(32, "little")


def _decode_point(encoded32: bytes) -> tuple:
    """Декодирует сжатую точку Edwards25519; ValueError для невалидной."""
    if len(encoded32) != 32:
        raise ValueError("точка должна быть 32 байта")
    raw = int.from_bytes(encoded32, "little")
    sign = (raw >> 255) & 1
    y = raw & ((1 << 255) - 1)
    if y >= _Q:
        raise ValueError("y вне поля")
    x = _xrecover(y)
    if (x & 1) != sign:
        x = _Q - x
    if (-x * x + y * y - 1 - _D * x * x * y * y) % _Q:
        raise ValueError("точка не на кривой")
    return (x, y, 1, x * y % _Q)


def ed25519_verify(signature64: bytes, message: bytes,
                   publickey32: bytes) -> bool:
    """Проверка подписи RFC 8032: [S]B == R + [H(R||A||M)]A."""
    if len(signature64) != 64 or len(publickey32) != 32:
        return False
    r_enc, s_bytes = signature64[:32], signature64[32:]
    s = int.from_bytes(s_bytes, "little")
    if s >= _L:  # non-canonical S отвергаем — иначе подпись можно мутировать
        return False
    try:
        r_point = _decode_point(r_enc)
        a_point = _decode_point(publickey32)
    except ValueError:
        return False
    h = int.from_bytes(
        hashlib.sha512(r_enc + publickey32 + message).digest(), "little") % _L
    left = _scalarmult(_B, s)
    right = _ed_add(r_point, _scalarmult(a_point, h))
    return _encodepoint(left) == _encodepoint(right)


# ------------------------------------------------- Borsh (фикс. структура)
# Borsh-сериализация near-primitives Transaction/SignedTransaction.
# Поля фиксированы под transfer — обобщать не требуется.


def _borsh_u32(v: int) -> bytes:
    return struct.pack("<I", v)


def _borsh_u64(v: int) -> bytes:
    return struct.pack("<Q", v)


def _borsh_u128(v: int) -> bytes:
    return v.to_bytes(16, "little")


def _borsh_string(s: str) -> bytes:
    b = s.encode("utf-8")
    return _borsh_u32(len(b)) + b


def _borsh_pubkey(pubkey32: bytes) -> bytes:
    """PublicKey = enum{keyType: u8 (0=ED25519)} + [u8;32]."""
    return b"\x00" + pubkey32


def serialize_transfer_tx(signer_id: str, pubkey32: bytes, nonce: int,
                          receiver_id: str, block_hash32: bytes,
                          deposit_yocto: int) -> bytes:
    """Transaction{signer_id, public_key, nonce, receiver_id,
    block_hash, actions:[Transfer{deposit}]} — Borsh."""
    action_transfer = b"\x03" + _borsh_u128(deposit_yocto)  # enum index 3 = Transfer
    return b"".join([
        _borsh_string(signer_id),
        _borsh_pubkey(pubkey32),
        _borsh_u64(nonce),
        _borsh_string(receiver_id),
        block_hash32,
        _borsh_u32(1),               # actions.len = 1
        action_transfer,
    ])


def serialize_signed_tx(tx_bytes: bytes, signature64: bytes) -> bytes:
    """SignedTransaction{transaction, signature{keyType=0, data[64]}}."""
    return tx_bytes + b"\x00" + signature64


def serialize_function_call_tx(signer_id: str, pubkey32: bytes, nonce: int,
                               receiver_id: str, block_hash32: bytes,
                               method_name: str, args_bytes: bytes,
                               gas: int, deposit_yocto: int) -> bytes:
    """Transaction{... actions:[FunctionCall{method,args,gas,deposit}]} — Borsh.

    NEAR Action enum: FunctionCall = 2. args — сырые байты JSON, не строка:
    Vec<u8> сериализуется как u32(len) + bytes.
    """
    action = b"".join([
        b"\x02",                     # enum index 2 = FunctionCall
        _borsh_string(method_name),
        _borsh_u32(len(args_bytes)),
        args_bytes,
        _borsh_u64(int(gas)),
        _borsh_u128(int(deposit_yocto)),
    ])
    return b"".join([
        _borsh_string(signer_id),
        _borsh_pubkey(pubkey32),
        _borsh_u64(nonce),
        _borsh_string(receiver_id),
        block_hash32,
        _borsh_u32(1),
        action,
    ])


class ContractCallError(RuntimeError):
    """Вызов контракта дошёл до сети, но исполнение упало (panic/host error)."""

    def __init__(self, reason: str, tx_hash: str = "",
                 logs: list[str] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.tx_hash = tx_hash
        self.logs = logs or []


def parse_near_private_key(key: str) -> tuple[bytes, bytes]:
    """NEAR_PRIVATE_KEY 'ed25519:<b58>' или '<b58>' → (seed32, pubkey32).

    Формат ключа NEAR: base58(seed32 || pubkey32). Если передан только
    seed (32 байта), pubkey выводится из seed.
    """
    raw = key.strip()
    if raw.startswith("ed25519:"):
        raw = raw[len("ed25519:"):]
    data = b58decode(raw)
    if len(data) == 64:
        return data[:32], data[32:]
    if len(data) == 32:
        return data, ed25519_publickey(data)
    raise ValueError(
        "NEAR_PRIVATE_KEY: base58 должен давать 64 байта "
        "(seed||pubkey) или 32 байта (seed) — получено "
        f"{len(data)}")


class NearTestnetClient:
    """Минимальный клиент NEAR Testnet: query + отправка через near-api-py."""

    def __init__(self, endpoint: str | None = None, timeout_s: float = 30.0):
        self.endpoint = (endpoint or env("NEAR_RPC_URL")
                         or DEFAULT_RPC).rstrip("/")
        self.timeout_s = timeout_s

    # ------------------------------------------------------------ raw RPC
    def call(self, method: str, params: dict) -> dict:
        """JSON-RPC 2.0 POST → result. Ошибка RPC — RuntimeError с телом."""
        payload = {
            "jsonrpc": "2.0", "id": "nexus-demo",
            "method": method, "params": params,
        }
        req = urllib.request.Request(
            self.endpoint, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            with e:
                body = e.read().decode("utf-8", "replace")[:300]
                raise RuntimeError(f"NEAR RPC HTTP {e.code}: {body}") from e
        if "error" in data:
            raise RuntimeError(f"NEAR RPC {method}: {data['error']}")
        return data.get("result", data)

    # ------------------------------------------------------------ queries
    def account_balance_yocto(self, account_id: str) -> int:
        """Баланс аккаунта в yoctoNEAR (view_account.amount)."""
        result = self.call("query", {
            "request_type": "view_account",
            "finality": "final",
            "account_id": account_id,
        })
        return int(result.get("amount", "0"))

    def access_key_nonce_and_hash(self, account_id: str,
                                  public_key: str) -> tuple[int, bytes]:
        """(nonce+1, block_hash) access key — готовая пара для подписи.

        Транзакция требует nonce = key.nonce + 1 и block_hash того блока,
        в котором был ответ — оба поля приходят в одном query.
        """
        result = self.call("query", {
            "request_type": "view_access_key",
            "finality": "final",
            "account_id": account_id,
            "public_key": public_key,
        })
        nonce = int(result["nonce"]) + 1
        block_hash = b58decode(result["block_hash"])
        if len(block_hash) != 32:
            raise RuntimeError(
                f"NEAR RPC: block_hash {len(block_hash)} байт, ожидалось 32")
        return nonce, block_hash

    def access_key_nonce(self, account_id: str, public_key: str) -> int:
        """nonce access key — нужен для подписи транзакции."""
        result = self.call("query", {
            "request_type": "view_access_key",
            "finality": "final",
            "account_id": account_id,
            "public_key": public_key,
        })
        return int(result["nonce"])

    # -------------------------------------------------------------- views
    def view_function(self, account_id: str, method_name: str,
                      args: dict | None = None) -> object:
        """call_function через query → распарсенный JSON (или строка)."""
        args_bytes = json.dumps(args or {}, separators=(",", ":")).encode()
        result = self.call("query", {
            "request_type": "call_function",
            "finality": "final",
            "account_id": account_id,
            "method_name": method_name,
            "args_base64": base64.b64encode(args_bytes).decode("ascii"),
        })
        raw = bytes(result.get("result", []) or [])
        text = raw.decode("utf-8", "replace")
        if not text:
            return None
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return text
        # Часть view-методов near-sdk отдаёт JSON-строку с вложенным JSON
        # (Option<(String,String,bool)>): декодируем второй слой.
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    def contract_deployed(self, account_id: str) -> bool:
        """True если у аккаунта есть код контракта (не пустой code_hash)."""
        result = self.call("query", {
            "request_type": "view_account",
            "finality": "final",
            "account_id": account_id,
        })
        code_hash = result.get("code_hash", "")
        return code_hash not in ("", "11111111111111111111111111111111")

    # ------------------------------------------------------------ transfer
    def _broadcast(self, signer_id: str, seed32: bytes, pubkey32: bytes,
                   build_tx) -> tuple[dict, bytes]:
        """Подпись + broadcast с ретраем на InvalidNonce.

        view_access_key/finality final может отставать от только что
        принятой транзакции; узел возвращает актуальный ak_nonce в ошибке —
        берём его и повторяем, а не падаем на гонке nonce.
        """
        pubkey_str = "ed25519:" + b58encode(pubkey32)
        nonce, block_hash = self.access_key_nonce_and_hash(
            signer_id, pubkey_str)
        last: RuntimeError | None = None
        for attempt in range(4):
            tx = build_tx(nonce, block_hash)
            signature = ed25519_sign(hashlib.sha256(tx).digest(),
                                     seed32, pubkey32)
            signed = serialize_signed_tx(tx, signature)
            try:
                res = self.call("broadcast_tx_commit", [
                    base64.b64encode(signed).decode("ascii")])
                return res, signed
            except RuntimeError as e:
                last = e
                m = re.search(r"'ak_nonce':\s*(\d+)", str(e))
                if m and attempt < 3:
                    nonce = int(m.group(1)) + 1
                    continue
                raise
        raise last or RuntimeError("broadcast: нет ответа")

    def send_transfer(self, signer_id: str, receiver_id: str,
                      amount_yocto: int, private_key: str) -> str:
        """Подписанный перевод NEAR → tx hash.

        Встроенная подпись: pure-Python ed25519 + Borsh — внешних
        зависимостей нет (near-api-py/py-near не собираются на новых
        Python). Шаги: view_access_key → nonce+1 + block_hash →
        Borsh(Transaction) → sha256 → ed25519-подпись хэша →
        SignedTransaction → broadcast_tx_commit(base64).
        """
        seed32, pubkey32 = parse_near_private_key(private_key)
        res, signed = self._broadcast(
            signer_id, seed32, pubkey32,
            lambda nonce, block_hash: serialize_transfer_tx(
                signer_id, pubkey32, nonce, receiver_id, block_hash,
                int(amount_yocto)))
        # Формат результата broadcast_tx_commit отличается между версиями —
        # вытаскиваем хэш защищённо, без молчаливой подмены.
        if isinstance(res, dict):
            for path in (
                ("transaction", "hash"),
                ("transaction_outcome", "id"),
                ("tx_hash",),
            ):
                cur: object = res
                for key in path:
                    cur = cur.get(key) if isinstance(cur, dict) else None
                if isinstance(cur, str) and cur:
                    return cur
            # Хэш подписанной транзакции — base58(sha256(bytes)); у нас он
            # вычислим локально, честнее чем "unknown-hash".
            return b58encode(hashlib.sha256(signed).digest())
        return str(res)

    # ----------------------------------------------------- function calls
    def send_function_call(self, signer_id: str, receiver_id: str,
                           method_name: str, args: dict,
                           private_key: str, gas: int = 50 * 10**12,
                           deposit_yocto: int = 0) -> dict:
        """Подписанный FunctionCall → {tx_hash, logs, status}.

        Успех — status="success". Panic контракта — ContractCallError с
        сырым текстом и логами (tx_hash уже существует в сети, его не
        прячем). RPC-сбой транспорта — RuntimeError.
        """
        seed32, pubkey32 = parse_near_private_key(private_key)
        args_bytes = json.dumps(args, separators=(",", ":")).encode()
        res, signed = self._broadcast(
            signer_id, seed32, pubkey32,
            lambda nonce, block_hash: serialize_function_call_tx(
                signer_id, pubkey32, nonce, receiver_id, block_hash,
                method_name, args_bytes, int(gas), int(deposit_yocto)))
        tx_hash = _result_tx_hash(res) or b58encode(
            hashlib.sha256(signed).digest())
        logs = _result_logs(res)
        status = res.get("status") if isinstance(res, dict) else None
        if isinstance(status, dict) and "Failure" in status:
            raise ContractCallError(
                _failure_text(status["Failure"]), tx_hash, logs)
        return {"tx_hash": tx_hash, "logs": logs, "status": "success"}


def _result_tx_hash(res: object) -> str | None:
    if not isinstance(res, dict):
        return None
    for path in (("transaction", "hash"), ("transaction_outcome", "id"),
                 ("tx_hash",)):
        cur: object = res
        for key in path:
            cur = cur.get(key) if isinstance(cur, dict) else None
        if isinstance(cur, str) and cur:
            return cur
    return None


def _result_logs(res: object) -> list[str]:
    """Логи всех receipts_outcome — судьям виден on-chain след контракта."""
    if not isinstance(res, dict):
        return []
    out: list[str] = []
    for item in res.get("receipts_outcome", []) or []:
        outcome = item.get("outcome", {}) if isinstance(item, dict) else {}
        out.extend(str(x) for x in outcome.get("logs", []) or [])
    return out


def _failure_text(failure: object) -> str:
    """Достаёт человекочитаемый panic/host error из status.Failure."""
    parts: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("ExecutionError", "HostError", "error_message",
                           "message") and isinstance(value, str):
                    parts.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(failure)
    if parts:
        return " | ".join(dict.fromkeys(parts))
    return json.dumps(failure, ensure_ascii=False)[:500]

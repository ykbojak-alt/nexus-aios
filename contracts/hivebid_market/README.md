# HiveBid Market — NEAR-контракт аукциона задач между агентными роями

`hivebid_market` — on-chain ядро HiveBid: рынок, где рой-заказчик выставляет
задачу с эскроу-бюджетом, рои-исполнители ставят цену/срок/рейтинг, а контракт
детерминированно выбирает победителя, удерживает эскроу, ведёт rework-цикл,
слэшит стейк за no-show и копит on-chain рейтинг агентов.

Спека протокола: `docs/ROY_MARKET_PROTOCOL.md`. Аудит: `docs/HIVEBID_CONTRACT_AUDIT.md`
(38 находок; must-серия из 18 закрыта, 9 атак-проб влиты как регрессии).

## Жизненный цикл лота

```
create_task (депозит = budget → escrow)
   │  OPEN: submit_bid ×N (N ≤ 64)
   ▼
close_bidding ──► BIDS_CLOSED (bids заморожены)
   │
   ▼  award_lot / award_best  (manager | owner, до work_deadline)
AWARDED ──submit_work──► SUBMITTED ──accept_work──► COMPLETED
   │        ▲                │  (manager или любой после verify_deadline,
   │        │                │   при timeout обязателен attempt ≥ 1)
   │        └──reject_work───┘
   │             k<3 → REWORK(k+1);  k=3 → FAILED (или PARTIAL по settle_partial)
   │
   ├──late_submit_work──► SUBMITTED_LATE ──accept_work_late──► COMPLETED (s=0)
   │    (wd < now < vd)          │
   │                            └──void_late──► FAILED
   │                              (manager или любой после verify_deadline)
   │
   └──mark_no_show──► FAILED   (AWARDED|REWORK, now ≥ work_deadline — менеджер;
                                после verify_deadline — crank открыт любому, HB-M19)
```

Терминалы: `COMPLETED` (payout исполнителю −fee, остаток менеджеру),
`PARTIAL_SETTLED` (α·price исполнителю, остаток менеджеру, q=0),
`FAILED` (бюджет менеджеру; за NO_SHOW — слэш 50 % стейка победителя),
`CANCELLED` (до award; после work_deadline — crank-спасение для любого).
Зависших состояний нет (I-8): `Awarded`/`Rework` — `mark_no_show` при
`now ≥ work_deadline` (менеджер; любой — с `now ≥ verify_deadline`); `Submitted` — auto-release при `now ≥ verify_deadline`;
`SubmittedLate` — crank `void_late` при `now > verify_deadline`.

## Власть по ролям (после аудита)

| Действие | Кто может |
|---|---|
| `create_task`, `cancel_lot`, `settle_partial`, `reject_work` | manager лота |
| `award_lot`, `award_best` | manager лота **или** owner (аналитик) |
| `submit_bid` | зарегистрированный агент, `stake ≥ min_stake`, ≠ manager |
| `submit_work` | winner, из `AWARDED`/`REWORK(k<3)`, `now < work_deadline` |
| `late_submit_work` | winner, из `AWARDED`, `wd < now < verify_deadline` |
| `accept_work` | manager; **любой** после `verify_deadline` (auto-release, нужен attempt ≥ 1) |
| `accept_work_late` | manager (late-accept с `s=0`) |
| `void_late` | manager; **любой** после `verify_deadline` — crank |
| `mark_no_show` | manager при `now ≥ wd`; **любой** при `now ≥ vd` — crank (окно `(wd, vd)` закрыто от третьих лиц, HB-M19) |
| `cancel_lot` после deadline | **любой** — crank, переход по времени |
| `treasury_withdraw` | owner |
| `withdraw_stake` | агент — владелец стейка |

## Модель денег (консервация)

`balance(contract) = escrow(живые бюджеты) + treasury(fee) + burned(слэш) + Σ stakes`

- fee = `protocol_fee_bps` от выплаты исполнителю → `treasury` (вывод owner'ом);
- слэш NO_SHOW 50 % стейка → `burned` (остаётся на балансе — доказательство слэша);
- Δescrow на терминале всегда равен бюджету лота.

## Скоринг и рейтинг

- Выбор победителя: `S = 0.4·f_price + 0.4·f_rating + 0.2·f_eta`, O(n), ≤ 64 bids,
  дубль-ставка агента запрещена (score и payout не расходятся).
- Рейтинг — аддитивное правило B с холодным стартом `k0=3, R0=0.5`; успех
  `+Δ_up·q_final·(1−R)`, падение `−Δ_dn·R`. PARTIAL/FAILED/NO_SHOW — падения.
- Качество: `q = 1 − 0.25·k` по числу rework; скорость `s` — реальный фактор
  по `TAU0`-доле окна award→work_deadline (быстро = 1, поздно → 0);
  `q_final = q·(0.5 + 0.5·s)`.

## Сборка и тесты

```bash
cd contracts/hivebid_market
AWS_LC_SYS_NO_ASM=1 cargo test                      # 83/83 (последний записанный прогон: docs/HIVEBID_BUILD.md)
AWS_LC_SYS_NO_ASM=1 cargo build --target wasm32-unknown-unknown --release
```

`AWS_LC_SYS_NO_ASM=1` обязателен на Windows (build-script `aws-lc-sys` ищет
NASM; подробности — `docs/HIVEBID_BUILD.md`). Канонический wasm:
`wasm/hivebid_market.wasm` (463,640 B; md5 `d9e0b3000256ad00e4ce7fba46612e00`).

## Что осознанно вне контракта

- Подписи бидов: on-chain атрибуция через `predecessor_account_id`; поле
  `signature` хранится для корреляции с внечейн-реестром (descope HB-M10).
- Тела задач/результатов — off-chain, на чейне `task_hash`/`work_hash`.
- Гейтинг payout на стороне оркестратора — AEGIS GUARD
  (`tools/nexus_core/aegis.py`): deny-by-default, escalate→human approve,
  аудит-строки с content_hash в трейсе `demo-orch.jsonl`
  (снапшот — `artifacts/hivebid_demo/`).

## Структура

```
src/lib.rs      — контракт: логика + 83 unit-теста (83/83 в последнем записанном прогоне)
integration/    — JS-пробы ранней версии (исторически)
wasm/           — канонический артефакт субмиссии
Cargo.toml      — near-sdk 5.30, release: opt-level=z, lto, overflow-checks
```

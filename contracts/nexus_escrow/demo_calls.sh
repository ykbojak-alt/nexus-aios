#!/usr/bin/env bash
# Полный жизненный цикл nexus_escrow v2 через near-cli (testnet).
#
#   bash contracts/nexus_escrow/demo_calls.sh <contract> <account>
#
# NB: rpc.testnet.near.org деприкейтнут — если near-cli отдаёт 429,
# используйте python contracts/nexus_escrow/chain_demo.py (pure-Python
# RPC через test.rpc.fastnear.com + видит текст паник контракта).
set -euo pipefail
C="${1:-nexus-escrow.yksunder.testnet}"
A="${2:-yksunder.testnet}"
cd "$(dirname "$0")/../.."

PUB=$(python -c "import json,os;print(json.load(open(os.path.expanduser('~/.near-credentials/testnet/$A.json')))['public_key'])")
DL=$(python -c "import time;print(int(time.time()*1000)+3600000)")

echo "══════ [0] БАЛАНСЫ ДО ══════"
near view "$C" get_escrow_balance '{}'      # живые бюджеты в эскроу
near view "$C" get_contract_balance '{}'    # сырой баланс аккаунта
near state "$C" | head -5                   # общее состояние аккаунта

echo "══════ [1] register_agent (stake 0.1 NEAR) ══════"
near call "$C" register_agent "{\"public_key\":\"$PUB\"}" --accountId "$A" --deposit 0.1
near view "$C" get_agent "{\"agent_id\":\"$A\"}"

echo "══════ [2] ERR_TASK_EXPIRED — bid на истёкшую задачу ══════"
near call "$C" create_task '{"task_id":"demo-expired","deadline_ms":1}' --accountId "$A" --deposit 0.05
SIG=$(python -c "
import sys,json,os; sys.path.insert(0,'tools')
import near_rpc
cred=json.load(open(os.path.expanduser('~/.near-credentials/testnet/$A.json')))
seed32, pk32 = near_rpc.parse_near_private_key(cred['private_key'])
print(near_rpc.b58encode(near_rpc.ed25519_sign(b'bid:demo-expired:$A:20000000000000000000000', seed32, pk32)))
")
# Ожидаем panic: ERR_TASK_EXPIRED (near-cli покажет «unreachable»,
# текст паники виден в Explorer / через chain_demo.py)
near call "$C" submit_bid "{\"task_id\":\"demo-expired\",\"price_yocto\":\"20000000000000000000000\",\"signature\":\"$SIG\"}" --accountId "$A" || echo "  → bid отклонён (deadline) — ОК"
near call "$C" refund_open_task '{"task_id":"demo-expired"}' --accountId "$A"

echo "══════ [3] happy path — эскроу 0.05 → 0, reputation +1 ══════"
near call "$C" create_task "{\"task_id\":\"demo-happy\",\"deadline_ms\":$DL}" --accountId "$A" --deposit 0.05
echo "── escrow после create_task:"
near view "$C" get_escrow_balance '{}'
SIG=$(python -c "
import sys,json,os; sys.path.insert(0,'tools')
import near_rpc
cred=json.load(open(os.path.expanduser('~/.near-credentials/testnet/$A.json')))
seed32, pk32 = near_rpc.parse_near_private_key(cred['private_key'])
print(near_rpc.b58encode(near_rpc.ed25519_sign(b'bid:demo-happy:$A:20000000000000000000000', seed32, pk32)))
")
near call "$C" submit_bid "{\"task_id\":\"demo-happy\",\"price_yocto\":\"20000000000000000000000\",\"signature\":\"$SIG\"}" --accountId "$A"
near call "$C" accept_bid "{\"task_id\":\"demo-happy\",\"agent_id\":\"$A\"}" --accountId "$A"
near call "$C" submit_work '{"task_id":"demo-happy","work_hash":"sha256:demo"}' --accountId "$A"
near call "$C" verify_and_release '{"task_id":"demo-happy"}' --accountId "$A"
echo "── escrow после release (ожидаем 0):"
near view "$C" get_escrow_balance '{}'
echo "── репутация (tasks_completed=1):"
near view "$C" get_agent "{\"agent_id\":\"$A\"}"

echo "══════ [4] slash — короткий дедлайн, стейк сгорает ══════"
SHORT_DL=$(python -c "import time;print(int(time.time()*1000)+75000)")
near call "$C" create_task "{\"task_id\":\"demo-slash\",\"deadline_ms\":$SHORT_DL}" --accountId "$A" --deposit 0.05
SIG=$(python -c "
import sys,json,os; sys.path.insert(0,'tools')
import near_rpc
cred=json.load(open(os.path.expanduser('~/.near-credentials/testnet/$A.json')))
seed32, pk32 = near_rpc.parse_near_private_key(cred['private_key'])
print(near_rpc.b58encode(near_rpc.ed25519_sign(b'bid:demo-slash:$A:20000000000000000000000', seed32, pk32)))
")
near call "$C" submit_bid "{\"task_id\":\"demo-slash\",\"price_yocto\":\"20000000000000000000000\",\"signature\":\"$SIG\"}" --accountId "$A"
near call "$C" accept_bid "{\"task_id\":\"demo-slash\",\"agent_id\":\"$A\"}" --accountId "$A"
echo "── sleep 80s до дедлайна..." && sleep 80
near call "$C" slash '{"task_id":"demo-slash"}' --accountId "$A"

echo "══════ ФИНАЛ ══════"
near view "$C" get_agent "{\"agent_id\":\"$A\"}"    # failed=1, active=false
near view "$C" get_burned '{}'                     # сожжённый стейк
near view "$C" get_escrow_balance '{}'             # 0 — всё разошлось
near view "$C" get_contract_balance '{}'

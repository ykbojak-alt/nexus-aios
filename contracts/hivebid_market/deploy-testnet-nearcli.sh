#!/usr/bin/env bash
# HiveBid Market — testnet deploy + smoke (near-cli 4.x синтаксис).
# Зеркало deploy-testnet.sh (near-cli-rs) для установленного near-cli 4.0.13.
# Аккаунты передаются env: MKT= MGR= AGT= (faucet-аккаунты без sub-account паттерна).
# Креды: C:\Users\BoJaK\.near-credentials\testnet\<account>.json (FullAccess).
#
#   MKT=hivebid-yksunder.testnet MGR=hivebid-manager-yks.testnet AGT=hivebid-agent-yks.testnet \
#     HIVEBID_WASM=wasm/hivebid_market.wasm ./deploy-testnet-nearcli.sh deploy
#   ... smoke | edge
set -euo pipefail

NS=${NS:-yksunder}
MKT=${MKT:-hivebid.$NS.testnet}
MGR=${MGR:-manager.$NS.testnet}
AGT=${AGT:-agent.$NS.testnet}
WASM=${HIVEBID_WASM:-$(dirname "$0")/wasm/hivebid_market.wasm}
LOT=${LOT:-demo-1}
GAS='30000000000000'   # 30 Tgas, как в оригинале
NOW() { date +%s%3N; }

# wait_ms <target_ms> — точное wall-clock ожидание до target (вместо blind
# sleep: margin к deadline ≥15с и адаптивно к реальному времени шагов;
# закрывает HERETIC D-1/R4 — sleep 305 при BID=+300s давал margin 5с).
wait_ms() {
  local target=$1 now
  while :; do
    now=$(NOW); [ "$now" -ge "$target" ] && return 0
    sleep 2
  done
}

# call_retry <signer> <method> <args-json> [deposit] [tries] — retry на
# RPC/сетевые сбои (chain timestamp может отставать от wall clock).
call_retry() {
  local signer=$1 method=$2 args=$3 dep=${4:-0} tries=${5:-5} i
  for i in $(seq 1 "$tries"); do
    if call "$signer" "$method" "$args" "$dep"; then return 0; fi
    echo ".. $method retry $i/$tries"; sleep 10
  done
  return 1
}

call() { # call <signer> <method> <args-json> [deposit NEAR]
  local signer=$1 method=$2 args=$3 dep=${4:-0}
  echo ">> $method ($signer) $args dep=$dep N"
  near call "$MKT" "$method" "$args" \
    --useAccount "$signer" --gas "$GAS" --deposit "$dep" --networkId testnet
}

view() { # view <method> <args-json>
  near view "$MKT" "$1" --args "$2" --networkId testnet
}

case "${1:-}" in
deploy)
  # D-2 idempotency/migration-guard: get_escrow отвечает ⇒ контракт уже
  # инициализирован. Blind `new` на существующем state → panic контракта.
  # Upgrade-путь: deploy wasm БЕЗ new (state сохраняется), требует
  # явного CONFIRM_UPGRADE=1 — защита от случайного re-init.
  if view get_escrow '{}' >/dev/null 2>&1; then
    echo "!! $MKT уже инициализирован (get_escrow отвечает)."
    if [ "${CONFIRM_UPGRADE:-0}" != "1" ]; then
      echo "   upgrade deploy: перезапусти с CONFIRM_UPGRADE=1 — wasm будет"
      echo "   обновлён, new НЕ вызовется (state сохранится)"
      exit 1
    fi
    near deploy "$MKT" "$WASM" --networkId testnet --force
    echo "== upgrade deploy OK; state сохранён, new пропущен =="
  else
    near deploy "$MKT" "$WASM" --networkId testnet --force
    call "$MKT" new "{\"owner_id\":\"$MGR\"}" 0
  fi
  ;;
smoke)
  N=$(NOW); BID=$((N+300000)); WD=$((N+1800000)); VD=$((N+3600000))
  call "$AGT" register_agent '{}' 10
  call "$MGR" create_task "{\"lot_id\":\"$LOT\",\"task_hash\":\"T:$(printf 'a%.0s' {1..64})\",\"bid_deadline_ms\":$BID,\"work_deadline_ms\":$WD,\"verify_deadline_ms\":$VD}" 50
  # W30: price — JSON-целое (u128) без кавычек, не строка.
  # Строка ломает десериализацию и вызывает wasm trap: unreachable.
  call "$AGT" submit_bid "{\"lot_id\":\"$LOT\",\"price\":30000000000000000000000000,\"eta_ms\":$WD,\"signature\":\"demo\"}"
  echo ".. жду bid_deadline +15s margin ($((BID + 15000)))"
  wait_ms $((BID + 15000))
  call_retry "$MGR" close_bidding "{\"lot_id\":\"$LOT\"}" 0 5
  call "$MGR" award_best "{\"lot_id\":\"$LOT\"}"
  call "$AGT" submit_work "{\"lot_id\":\"$LOT\",\"work_hash\":\"W:demo\"}"
  call "$MGR" accept_work "{\"lot_id\":\"$LOT\"}"
  echo "== views =="
  view get_lot "{\"lot_id\":\"$LOT\"}"
  view get_agent "{\"account\":\"$AGT\"}"
  ;;
edge)
  N=$(NOW); BID=$((N+120000)); WD=$((N+420000)); VD=$((N+900000))
  call "$MGR" create_task "{\"lot_id\":\"edge-1\",\"task_hash\":\"T:$(printf 'b%.0s' {1..64})\",\"bid_deadline_ms\":$BID,\"work_deadline_ms\":$WD,\"verify_deadline_ms\":$VD}" 50
  call "$AGT" submit_bid "{\"lot_id\":\"edge-1\",\"price\":30000000000000000000000000,\"eta_ms\":$WD,\"signature\":\"edge\"}"
  echo ".. жду bid_deadline +15s margin"; wait_ms $((BID + 15000))
  call_retry "$MGR" close_bidding '{"lot_id":"edge-1"}' 0 5
  call "$MGR" award_best '{"lot_id":"edge-1"}'
  # D-6: mark_no_show требует now >= work_deadline — ждём WD+15s по
  # wall-clock (sleep 400 гонялся с реальным WD при медленных шагах)
  echo ".. жду work_deadline +15s ($((WD + 15000))) — mark_no_show"
  wait_ms $((WD + 15000))
  call_retry "$MGR" mark_no_show '{"lot_id":"edge-1"}' 0 5
  view get_lot '{"lot_id":"edge-1"}'
  ;;
*) echo "usage: $0 deploy|smoke|edge" >&2; exit 2 ;;
esac

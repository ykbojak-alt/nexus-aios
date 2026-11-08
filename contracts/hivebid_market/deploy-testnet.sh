#!/usr/bin/env bash
# HiveBid Market — testnet deploy + smoke run.
# Реализация docs/HIVEBID_DEPLOY.md как один прогон.
# Кошельки создаёт ВЛАДЕЛЕЦ (правило роев) — скрипт только вызывает.
#
# Требуется: near-cli-rs (`cargo install near-cli-rs`), входы в keychain:
#   <NS>.testnet (твой), hivebid.<NS>, manager.<NS>, agent.<NS>
#
# Использование:
#   NS=<you> ./deploy-testnet.sh deploy   # деплой wasm + init(owner=manager)
#   NS=<you> ./deploy-testnet.sh smoke    # happy-path: register→…→accept
#   NS=<you> ./deploy-testnet.sh edge     # no_show/late/void демо-ветки
set -euo pipefail

NS=${NS:-yksunder}
# Аккаунты можно задать явно (фаянс-аккаунты без sub-account паттерна):
#   MKT=hivebid-yksunder.testnet MGR=hivebid-manager-yks.testnet AGT=hivebid-agent-yks.testnet ./deploy-testnet.sh deploy
MKT=${MKT:-hivebid.$NS.testnet}
MGR=${MGR:-manager.$NS.testnet}
AGT=${AGT:-agent.$NS.testnet}
WASM=${HIVEBID_WASM:-$(dirname "$0")/wasm/hivebid_market.wasm}
LOT=${LOT:-demo-1}
NET="network-config testnet sign-with-keychain send"
NOW() { date +%s%3N; }

# wait_ms <target_ms> — точное wall-clock ожидание до target (вместо blind
# sleep: margin к deadline ≥15с, адаптивно; HERETIC D-1/R4/D-6).
wait_ms() {
  local target=$1 now
  while :; do
    now=$(NOW); [ "$now" -ge "$target" ] && return 0
    sleep 2
  done
}

# call_retry <signer> <method> <args> [deposit] [tries]
call_retry() {
  local signer=$1 method=$2 args=$3 dep=${4:-0} tries=${5:-5} i
  for i in $(seq 1 "$tries"); do
    if call "$signer" "$method" "$args" "$dep"; then return 0; fi
    echo ".. $method retry $i/$tries"; sleep 10
  done
  return 1
}

call() { # call <signer> <method> <args> [deposit]
  local signer=$1 method=$2 args=$3 dep=${4:-0}
  echo ">> $method ($signer) $args dep=$dep N"
  near contract call-function as-transaction "$MKT" "$method" \
    json-args "$args" prepaid-gas '30.0 Tgas' \
    attached-deposit "$dep NEAR" sign-as "$signer" $NET
}

view() { # view <method> <args-json> — read-only probe для guard'ов
  near contract call-function as-read-only "$MKT" "$1" \
    json-args "$2" network-config testnet now
}

case "${1:-}" in
deploy)
  # D-2 idempotency/migration-guard: get_escrow отвечает ⇒ state уже есть.
  # Blind `new` на существующем state → panic. Upgrade = deploy БЕЗ new,
  # требует CONFIRM_UPGRADE=1.
  if view get_escrow '{}' >/dev/null 2>&1; then
    echo "!! $MKT уже инициализирован (get_escrow отвечает)."
    if [ "${CONFIRM_UPGRADE:-0}" != "1" ]; then
      echo "   upgrade deploy: перезапусти с CONFIRM_UPGRADE=1 — wasm обновится,"
      echo "   new НЕ вызовется (state сохранится)"
      exit 1
    fi
    near contract deploy "$MKT" use-file "$WASM" \
      without-init-call network-config testnet sign-with-keychain send
    echo "== upgrade deploy OK; state сохранён, new пропущен =="
  else
    near contract deploy "$MKT" use-file "$WASM" \
      without-init-call network-config testnet sign-with-keychain send
    call "$MKT" new "{\"owner_id\":\"$MGR\"}"
  fi
  ;;
smoke)
  N=$(NOW); BID=$((N+300000)); WD=$((N+1800000)); VD=$((N+3600000))
  call "$AGT" register_agent '{}' 10
  call "$MGR" create_task "{\"lot_id\":\"$LOT\",\"task_hash\":\"T:$(printf 'a%.0s' {1..64})\",\"bid_deadline_ms\":$BID,\"work_deadline_ms\":$WD,\"verify_deadline_ms\":$VD}" 50
  call "$AGT" submit_bid "{\"lot_id\":\"$LOT\",\"price\":\"30000000000000000000000000\",\"eta_ms\":$WD,\"signature\":\"demo\"}"
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
  call "$AGT" submit_bid "{\"lot_id\":\"edge-1\",\"price\":\"30000000000000000000000000\",\"eta_ms\":$WD,\"signature\":\"edge\"}"
  echo ".. жду bid_deadline +15s margin"; wait_ms $((BID + 15000))
  call_retry "$MGR" close_bidding '{"lot_id":"edge-1"}' 0 5
  call "$MGR" award_best '{"lot_id":"edge-1"}'
  # D-6: mark_no_show требует now >= work_deadline — WD+15s wall-clock
  echo ".. жду work_deadline +15s ($((WD + 15000))) — mark_no_show"
  wait_ms $((WD + 15000))
  call_retry "$MGR" mark_no_show '{"lot_id":"edge-1"}' 0 5
  echo "edge OK: no_show → Failed, stake slashed 50%"
  ;;
*)
  echo "usage: NS=<you> $0 {deploy|smoke|edge}" >&2; exit 2;;
esac

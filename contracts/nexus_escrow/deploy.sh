#!/usr/bin/env bash
# nexus_escrow — сборка + деплой на NEAR Testnet.
#
#   bash contracts/nexus_escrow/deploy.sh <account> <master_account>
#
# Пример (саб-аккаунт под вашим testnet-аккаунтом):
#   bash contracts/nexus_escrow/deploy.sh nexus-escrow.yksunder.testnet yksunder.testnet
#
# Требования: rustup + wasm32-unknown-unknown, near-cli (npm i -g near-cli),
# credentials мастер-аккаунта в ~/.near-credentials/testnet/<master>.json.
# Топ-уровневый <name>.testnet создаётся через флаг --useFaucet вместо
# --masterAccount (helper.testnet.near.org — если жив).
set -euo pipefail

CONTRACT_DIR="$(cd "$(dirname "$0")" && pwd)"
WASM="$CONTRACT_DIR/target/wasm32-unknown-unknown/release/nexus_escrow.wasm"
ACCOUNT="${1:?укажи contract account, напр. nexus-escrow.yksunder.testnet}"
MASTER="${2:-}"
FUND="${FUND:-2.4}"   # NEAR на storage wasm (~1.8) + стейт

echo "== [1/3] build wasm =="
rustup target list --installed | grep -q wasm32-unknown-unknown \
    || rustup target add wasm32-unknown-unknown
cargo build --manifest-path "$CONTRACT_DIR/Cargo.toml" \
    --target wasm32-unknown-unknown --release
ls -la "$WASM"

echo "== [2/3] create account $ACCOUNT =="
if near view "$ACCOUNT" get_burned '{}' >/dev/null 2>&1; then
    echo "   уже существует и контракт жив — пропуск"
elif [[ -n "$MASTER" ]]; then
    # Новый keypair генерируется снаружи (tools/near_rpc.py ed25519_publickey)
    # и кладётся в ~/.near-credentials/testnet/$ACCOUNT.json до запуска.
    PUB=$(python -c "import json,os;print(json.load(open(os.path.expanduser('~/.near-credentials/testnet/$ACCOUNT.json')))['public_key'])")
    near create-account "$ACCOUNT" --masterAccount "$MASTER" \
         --initialBalance "$FUND" --publicKey "$PUB"
else
    near create-account "$ACCOUNT" --useFaucet --publicKey "$PUB"
fi

echo "== [3/3] deploy + init =="
near deploy "$ACCOUNT" "$WASM" --initFunction new --initArgs '{}' \
    --networkId testnet
echo "DONE: contract живёт на $ACCOUNT"

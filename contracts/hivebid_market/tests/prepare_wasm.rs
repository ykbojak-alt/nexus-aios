//! Regression gate + diagnostic: the canonical wasm artifact must survive
//! near-vm contract preparation — the exact check neard runs before executing
//! any call. Reproduces sandbox `CompilationError::PrepareError::Deserialization`
//! locally and pinpoints the rejecting feature via wasmparser directly.
use near_parameters::{vm::VMKind, RuntimeConfigStore};
use near_primitives_core::types::ProtocolVersion;
use near_vm_runner::prepare::prepare_contract;

/// Конфиг целевой сети: testnet protocol 88 (RPC status 2026-11-06).
/// `initial_testnet_config()` — базовый (pre-diff) конфиг, там
/// `reftypes_bulk_memory=false` и wasm с bulk-memory опкодами (LLVM
/// memcpy lowering) НЕ проходит prepare — это НЕ контрактный дефект.
const TARGET_PROTOCOL: ProtocolVersion = 88;

#[test]
fn canonical_wasm_passes_near_vm_prepare() {
    let wasm = include_bytes!("../wasm/hivebid_market.wasm");
    let store = RuntimeConfigStore::for_chain_id("testnet");
    let runtime = store.get_config(TARGET_PROTOCOL);
    let vm_config = (*runtime.wasm_config).clone();
    eprintln!("reftypes_bulk_memory={}", vm_config.reftypes_bulk_memory);

    // Stage 0 — sanity: module parses under wasmparser with ALL features.
    let mut v = wasmparser::Validator::new();
    match v.validate_all(wasm) {
        Ok(_) => eprintln!("wasmparser[all-features]: OK"),
        Err(e) => panic!("wasmparser[all-features] FAIL: {e}"),
    }

    // Stage 1 — replicate near-vm's feature set (features.rs opts) and find
    // the exact rejecting feature/opcode.
    let rt = vm_config.reftypes_bulk_memory;
    let f = wasmparser::WasmFeaturesInflated {
        floats: true,
        mutable_global: true,
        sign_extension: true,
        reference_types: rt,
        bulk_memory: rt,
        multi_value: false,
        simd: false,
        threads: false,
        shared_everything_threads: false,
        tail_call: false,
        multi_memory: false,
        exceptions: false,
        legacy_exceptions: false,
        memory64: false,
        saturating_float_to_int: true,
        relaxed_simd: false,
        extended_const: false,
        component_model: false,
        cm_values: false,
        cm_nested_names: false,
        cm_async: false,
        cm_async_stackful: false,
        cm_async_builtins: false,
        function_references: false,
        memory_control: false,
        gc: false,
        gc_types: false,
        custom_page_sizes: false,
        stack_switching: false,
        wide_arithmetic: false,
    };
    let _ = f;
    let mut v2 = wasmparser::Validator::new_with_features(f.into());
    match v2.validate_all(wasm) {
        Ok(_) => eprintln!("wasmparser[near-features]: OK"),
        Err(e) => eprintln!("wasmparser[near-features] FAIL: {e}"),
    }

    // Stage 2 — the real gate.
    for kind in [VMKind::Wasmtime, VMKind::NearVm] {
        let result = prepare_contract(wasm, &vm_config, kind);
        match &result {
            Ok(code) => eprintln!("kind={kind:?}: prepare OK, {} bytes", code.len()),
            Err(e) => eprintln!("kind={kind:?}: prepare ERR {e:?}"),
        }
        assert!(result.is_ok(), "near-vm prepare failed for {kind:?}: {result:?}");
    }
}

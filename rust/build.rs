// Build script для Aionet Tauri:
//   1. Генерирует Rust-биндинги из proto/messages.proto через prost-build
//   2. Вызывает tauri-build для генерации контекста приложения
//
// Использует protoc-bin-vendored — не требует системной установки protoc.

use std::path::PathBuf;

fn main() {
    // ── 0. Vendored protoc (не требует apt install protobuf-compiler) ──
    let protoc = protoc_bin_vendored::protoc_bin_path()
        .expect("protoc-bin-vendored: failed to get protoc binary path");
    std::env::set_var("PROTOC", protoc);

    // ── 1. Tauri build ──
    tauri_build::build();

    // ── 2. Protobuf generation ──
    // proto/messages.proto лежит в корне проекта (один уровень вверх от rust/)
    let manifest_dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let proto_root = manifest_dir.join("..").join("proto");
    let proto_file = proto_root.join("messages.proto");

    if !proto_file.exists() {
        panic!(
            "proto/messages.proto not found at {}. \
             Run from project root or check CARGO_MANIFEST_DIR.",
            proto_file.display()
        );
    }

    // OUT_DIR — куда cargo кладёт сгенерированные файлы
    let out_dir = PathBuf::from(std::env::var("OUT_DIR").unwrap());

    // prost-build генерирует Rust-модуль (по package из .proto → aionet.v1.rs)
    let mut config = prost_build::Config::new();
    config.out_dir(&out_dir);

    // Клонируем proto_file т.к. он ещё нужен для rerun-if-changed
    let proto_file_for_compile = proto_file.clone();
    if let Err(e) = config.compile_protos(&[proto_file_for_compile], &[proto_root]) {
        panic!("prost-build failed: {e}");
    }

    // Сообщаем cargo, что нужно перегенерировать при изменении .proto
    println!("cargo:rerun-if-changed={}", proto_file.display());

    // Сгенерированный файл: $OUT_DIR/aionet.v1.rs (по package из .proto)
    let generated = out_dir.join("aionet.v1.rs");
    if !generated.exists() {
        panic!(
            "prost-build did not generate expected file: {}",
            generated.display()
        );
    }
    // Передаём путь к сгенерированному файлу в src/proto.rs через env
    println!("cargo:rustc-env=PROTO_GEN_PATH={}", generated.display());
}

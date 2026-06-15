#!/usr/bin/env bash
# build-llama-cpp.sh
#
# Compila llama.cpp com suporte RPC em CPUs antigas SEM BMI2 (Sandy/Ivy Bridge,
# i.e. Intel pré-2013). GCC moderno emite instruções `shlx` (BMI2) por padrão
# em -march=native — o binário crasha com SIGILL em CPUs Sandy Bridge.
#
# Validado em Intel i7-2620M (2011), flags suportadas: avx sse sse2 ssse3.
# Resultado: rpc-server, llama-cli, llama-server funcionais com 9 tok/s em
# cluster de 3 nós Qwen 2.5 1.5B Q4.
#
# Uso:  bash build-llama-cpp.sh [/opt/llama.cpp]

set -euo pipefail

PREFIX="${1:-/opt/llama.cpp}"

apt-get update
apt-get install -y --no-install-recommends \
    git build-essential cmake libcurl4-openssl-dev pkg-config

if [[ ! -d "$PREFIX" ]]; then
    git clone --depth 1 https://github.com/ggerganov/llama.cpp "$PREFIX"
fi

cd "$PREFIX"
mkdir -p build && cd build

# Flags críticas:
#   -DGGML_RPC=ON          → habilita backend RPC
#   -DGGML_NATIVE=OFF      → desliga detecção automática (-march=native)
#   -DGGML_BMI2=OFF        → essencial: instruções shlx crasham Sandy Bridge
#   -mno-bmi -mno-bmi2     → garante que GCC não emita BMI mesmo com -march
#   -march=sandybridge     → trava ISA exata da CPU
cmake .. \
    -DGGML_RPC=ON \
    -DGGML_NATIVE=OFF \
    -DGGML_AVX=ON \
    -DGGML_AVX2=OFF \
    -DGGML_AVX512=OFF \
    -DGGML_FMA=OFF \
    -DGGML_F16C=OFF \
    -DGGML_BMI2=OFF \
    -DLLAMA_CURL=OFF \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_C_FLAGS="-march=sandybridge -mno-bmi -mno-bmi2 -mno-avx2 -mno-fma -mno-f16c" \
    -DCMAKE_CXX_FLAGS="-march=sandybridge -mno-bmi -mno-bmi2 -mno-avx2 -mno-fma -mno-f16c"

cmake --build . --config Release -j"$(nproc)" --target rpc-server llama-cli llama-server

echo
echo "Binários gerados:"
ls -lh bin/{rpc-server,llama-cli,llama-server} 2>/dev/null || true
echo
echo "OK"

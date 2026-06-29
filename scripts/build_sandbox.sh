#!/usr/bin/env bash
# Сборка Docker-образа песочницы инструментов.
set -euo pipefail
cd "$(dirname "$0")/.."

# Генерируем protobuf-биндинги заранее, чтобы они были доступны в образе.
mkdir -p proto/_gen
python -m grpc_tools.protoc -Iproto --python_out=proto/_gen proto/messages.proto || {
  echo "WARNING: protobuf generation failed; runtime fallback will be used." >&2
}

docker build -t aionet-toolbox:latest -f docker/Dockerfile.toolbox .
echo "OK: aionet-toolbox:latest built"

# Установка AppArmor-профиля (только на Linux с apparmor)
if command -v apparmor_parser >/dev/null 2>&1; then
  sudo apparmor_parser -r docker/apparmor-profile 2>/dev/null || \
    echo "WARNING: AppArmor profile install skipped (sudo not available or apparmor not active)"
else
  echo "NOTE: apparmor_parser not found; seccomp-only mode"
fi

echo ""
echo "To verify, run:"
echo "  docker run --rm --security-opt seccomp=docker/seccomp-profile.json \\"
echo "       --security-opt apparmor=aionet-toolbox \\"
echo "       --read-only --network none --user 1000:1000 \\"
echo "       -v \$(pwd)/workspace:/workspace:rw aionet-toolbox:latest \\"
echo "       python -c 'import os; print(os.uname()); print(os.listdir(\"/workspace\"))'"

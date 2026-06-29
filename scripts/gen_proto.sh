#!/usr/bin/env bash
# Генерирует messages_pb2.py из proto/messages.proto.
# Используется на этапе сборки; если пропустить — common.proto сгенерирует на лету.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p proto/_gen
python -m grpc_tools.protoc -Iproto --python_out=proto/_gen proto/messages.proto
echo "OK: proto/_gen/messages_pb2.py"

#!/usr/bin/env bash
# 一键构建并启动磁盘健康页面（Docker）
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PORT="${DISK_HEALTH_PORT:-18089}"
NAME="disk-health"
IMAGE="disk-health:1.0.0"

if ! command -v docker >/dev/null 2>&1; then
  echo "未找到 docker，请先在 fnOS 应用中心安装 Docker。" >&2
  exit 1
fi

# 在受限环境（只读 HOME）下给 buildx 一个可写目录
if [ ! -w "${DOCKER_CONFIG:-$HOME/.docker}" ]; then
  export DOCKER_CONFIG="${TMPDIR:-/tmp}/disk-health-docker"
  mkdir -p "$DOCKER_CONFIG"
fi

mkdir -p "$ROOT/data"

echo "==> 构建镜像 $IMAGE"
docker build -t "$IMAGE" "$ROOT"

echo "==> 停止旧容器（如存在）"
docker rm -f "$NAME" >/dev/null 2>&1 || true

echo "==> 启动容器，端口 $PORT"
docker run -d \
  --name "$NAME" \
  --restart unless-stopped \
  --privileged \
  --uts=host \
  --add-host=host.docker.internal:host-gateway \
  -p "$PORT:8080" \
  -v /dev:/dev \
  -v "$ROOT/data:/data" \
  -e TZ=Asia/Shanghai \
  -e REFRESH_INTERVAL=120 \
  -e FNOS_BASE_URL=http://host.docker.internal:5666 \
  "$IMAGE" >/dev/null

echo
echo "已启动。访问地址："
DEF_IF="$(ip route show default 2>/dev/null | awk '{print $5; exit}')"
if [ -n "$DEF_IF" ]; then
  ip -4 -o addr show dev "$DEF_IF" scope global 2>/dev/null | awk -v p="$PORT" '{split($4, a, "/"); print "  http://" a[1] ":" p}'
else
  hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.' | awk -v p="$PORT" '{print "  http://" $1 ":" p}'
fi
echo "  本机: http://127.0.0.1:$PORT"
echo
echo "首次打开会跳转 fnOS 登录；登录后采集约 10-40 秒，若提示“正在采集”请稍候刷新。"
echo "查看日志: docker logs -f $NAME"

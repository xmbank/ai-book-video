#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if ! command -v node >/dev/null 2>&1; then
  NVM_ROOT="${NVM_DIR:-$HOME/.nvm}"
  for candidate in "$NVM_ROOT"/versions/node/*/bin; do
    if [[ -x "$candidate/node" ]]; then
      export PATH="$candidate:$PATH"
    fi
  done
fi

if ! command -v node >/dev/null 2>&1; then
  BUNDLED_NODE_DIR="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin"
  if [[ -x "$BUNDLED_NODE_DIR/node" ]]; then
    export PATH="$BUNDLED_NODE_DIR:$PATH"
  fi
fi

if ! command -v node >/dev/null 2>&1; then
  echo "未找到 Node.js，无法启动 HyperFrames 渲染环境" >&2
  exit 1
fi

if [[ ! -x .venv/bin/book-video-web ]]; then
  echo "Python 环境未安装，正在初始化..."
  uv sync --python 3.12 --extra media --extra dev
fi

if [[ ! -x node_modules/.bin/hyperframes ]]; then
  echo "Node.js 依赖未安装，正在初始化..."
  if command -v npm >/dev/null 2>&1; then
    npm install
  else
    echo "未找到 npm，无法首次安装 Node.js 依赖" >&2
    exit 1
  fi
fi

if [[ ! -f dist/index.html ]] || find frontend -type f -newer dist/index.html -print -quit | grep -q .; then
  echo "Web 前端尚未构建，正在构建..."
  if command -v npm >/dev/null 2>&1; then
    npm run build
  elif [[ -f node_modules/vite/bin/vite.js ]]; then
    node node_modules/vite/bin/vite.js build
  else
    echo "未找到 npm 或本地 Vite，无法构建 Web 前端" >&2
    exit 1
  fi
fi

HOST="${WORKBENCH_HOST:-127.0.0.1}"
if [[ -n "${WORKBENCH_PORT:-}" ]]; then
  PORT="$WORKBENCH_PORT"
else
  PORT=""
  for candidate in $(seq 8765 8795); do
    health="$(curl --silent --max-time 1 "http://127.0.0.1:${candidate}/api/health" 2>/dev/null || true)"
    if [[ "$health" == *'"service":"ai-book-video-workbench"'* ]]; then
      echo "图书视频工作台已在运行: http://127.0.0.1:${candidate}"
      exit 0
    fi
    if ! lsof -nP -iTCP:"${candidate}" -sTCP:LISTEN >/dev/null 2>&1; then
      PORT="$candidate"
      break
    fi
  done
fi

if [[ -z "$PORT" ]]; then
  echo "未找到可用端口（已检查 8765-8795）" >&2
  exit 1
fi

export WORKBENCH_HOST="$HOST"
export WORKBENCH_PORT="$PORT"
echo "图书视频工作台: http://${HOST}:${PORT}"
exec .venv/bin/book-video-web

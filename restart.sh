#!/bin/bash
cd "/Users/fatatalia/project/open-xiaoai-bridge"

# 清除代理环境变量，避免影响本地 WebSocket 直连
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY no_proxy NO_PROXY

# 加载环境变量
PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin"
export PATH
source "$HOME/.local/bin/env" 2>/dev/null

# 设置 DYLD_LIBRARY_PATH 供 sherpa-onnx 找 onnxruntime
ONNX_LIB_DIR="$(uv run python -c "from pathlib import Path; import onnxruntime; print(Path(onnxruntime.__file__).parent / 'capi')" 2>/dev/null)"
[ -n "$ONNX_LIB_DIR" ] && export DYLD_LIBRARY_PATH="${ONNX_LIB_DIR}:${DYLD_LIBRARY_PATH:+$DYLD_LIBRARY_PATH}"

# 1. 定义程序特征名（用于唯一识别进程）
APP_NAME="open-xiaoai-bridge"

# 2. 查找进程PID
# 通过 API Server 监听端口（9092）检测，比 grep 命令名更可靠
PID=$(lsof -ti :9092 2>/dev/null)

# 3. 检查进程是否存在
if [ -z "$PID" ]; then
    echo ">> $APP_NAME 尚未运行."
else
    echo ">> 找到 $APP_NAME 进程 (PID: $PID), 正在停止..."
    kill -9 $PID 2>/dev/null
    # 等待端口释放（最多5秒）
    for i in 1 2 3 4 5; do
        if lsof -ti :9092 >/dev/null 2>&1; then
            sleep 1
        else
            break
        fi
    done
    echo ">> $APP_NAME 已停止."
fi

echo "Starting Application..."

export API_SERVER_ENABLE=1
export API_SERVER_HOST=0.0.0.0
export DSH_ENABLE=1
export LOGLEVEL=debug
export DYLD_LIBRARY_PATH

# 统一用 disown 启动，避免 nohup 与 disown 的行为差异
./scripts/start.sh < /dev/null > ./log/api.log 2>&1 &
disown
echo "Application started."

#!/bin/bash
# XiaoAI Bridge 守护脚本
# 用法: nohup bash watchdog.sh > /dev/null 2>&1 &
# 检查 bridge 进程，挂了就自动拉起来

BRIDGE_DIR="/Users/fatatalia/open-xiaoai-bridge"
LOG_FILE="$BRIDGE_DIR/log/watchdog.log"
CHECK_INTERVAL=30

mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

check_bridge() {
    # 只匹配 open-xiaoai-bridge 目录下的 python 进程
    pgrep -f "[o]pen-xiaoai-bridge.*python3.*main\.py" > /dev/null 2>&1
}

start_bridge() {
    cd "$BRIDGE_DIR" || exit 1
    nohup bash -c "API_SERVER_ENABLE=1 API_SERVER_HOST=0.0.0.0 OPENCLAW_ENABLE=1 LOGLEVEL=debug ./scripts/start.sh" \
        > ./log/api.log 2>&1 &
    log "Bridge 未运行，已启动 (PID: $!)"
}

log "========== WatchDog 启动 =========="

# 首次检查
if check_bridge; then
    log "Bridge 已在运行，进入监控模式"
else
    start_bridge
fi

# 监控循环
while true; do
    sleep $CHECK_INTERVAL
    if ! check_bridge; then
        start_bridge
    fi
done

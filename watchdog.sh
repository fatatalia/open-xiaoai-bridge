#!/bin/bash
# XiaoAI Bridge 守护脚本
# 用法: nohup bash watchdog.sh > /dev/null 2>&1 &
# 检查 bridge 进程，挂了就自动拉起来
#
# ⚠️⚠️ 已废弃，不要再启动（2026-10-28 标记）⚠️⚠️
#
#   bridge 现在由 launchd 管理：
#     system/com.fatatalia.xiaoai-bridge   （KeepAlive 自动拉起）
#     查看: launchctl print system/com.fatatalia.xiaoai-bridge
#     重启: launchctl kickstart -k system/com.fatatalia.xiaoai-bridge
#
#   两个具体原因让本脚本不能再用：
#   1. start_bridge 用 nohup 起的是**脱离 launchd 的裸进程**。bridge 一旦崩，
#      launchd 和本脚本会各拉一个，两个进程抢 9092 端口。
#   2. BRIDGE_DIR 还指着老的空壳目录 /Users/fatatalia/open-xiaoai-bridge
#      （那里只有一个 openclaw/device.json），真实代码在
#      /Users/fatatalia/project/open-xiaoai-bridge。
#      ⇒ cd 过去找不到 scripts/start.sh，start_bridge 必然失败。
#
#   保留本文件仅供追溯历史，真实守护逻辑见 launchd plist。
#   （此路径 bug 刻意不修：修好反而会真的跑起来，制造双进程。）

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

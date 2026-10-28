#!/bin/bash
# XiaoAI Bridge 启动脚本（手动入口）
#
# 2026-10-28 重写。
# 旧版是 nohup 起一个脱离 launchd 的裸进程。bridge 现在由 launchd 托管
# （system/com.fatatalia.xiaoai-bridge，RunAtLoad + KeepAlive），
# 另起裸进程会和它抢 9092 端口。
#
# 注意：scripts/start.sh 是 launchd 真正执行的那个，
#       本文件只是给人用的入口，不要把它和 scripts/start.sh 搞混。
#
# 用法: ./start.sh

set -u

LABEL="system/com.fatatalia.xiaoai-bridge"
PLIST="/Library/LaunchDaemons/com.fatatalia.xiaoai-bridge.plist"

# ── job 已加载？──
if launchctl print "$LABEL" >/dev/null 2>&1; then
    # 已加载但可能没在跑（比如 bootout 过又 bootstrap，或进程崩了）
    if launchctl print "$LABEL" 2>/dev/null | grep -q "state = running"; then
        PID=$(launchctl print "$LABEL" 2>/dev/null | awk '$1 == "pid" && $2 == "=" {print $3; exit}')
        echo ">> 已在运行 (PID: ${PID:-未知})，无需启动"
        echo "   要重启请用: ./restart.sh"
        exit 0
    fi
    echo ">> job 已加载但未运行，kickstart 拉起 ..."
    launchctl kickstart "$LABEL" || { echo "❌ kickstart 失败"; exit 1; }
else
    echo ">> job 未加载，从 plist 载入 ..."
    if [ ! -f "$PLIST" ]; then
        echo "❌ plist 不存在: $PLIST"
        exit 1
    fi
    launchctl bootstrap system "$PLIST" || { echo "❌ bootstrap 失败"; exit 1; }
fi

# ── 等进程起来（最多 15 秒）──
for _ in $(seq 1 15); do
    sleep 1
    PID=$(launchctl print "$LABEL" 2>/dev/null | awk '$1 == "pid" && $2 == "=" {print $3; exit}')
    if [ -n "${PID:-}" ]; then
        echo ">> 已启动，PID: $PID"
        for _ in $(seq 1 10); do
            if lsof -ti :9092 >/dev/null 2>&1; then
                echo ">> 9092 端口已监听"
                exit 0
            fi
            sleep 1
        done
        echo "⚠️  进程起来了，但 9092 还没监听（可能仍在初始化）"
        exit 0
    fi
done

echo "⚠️  15 秒内没检测到进程"
echo "   看日志: /Users/fatatalia/project/open-xiaoai-bridge/log/api.out.log"
launchctl print "$LABEL" 2>/dev/null | grep -E "state = |pid = |last exit" | sed 's/^/   /'
exit 1

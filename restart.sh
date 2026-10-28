#!/bin/bash
# XiaoAI Bridge 重启脚本
#
# 2026-10-28 重写。
# 旧版是 kill -9 掉 9092 上的进程，再 nohup 起一个裸进程 ——
# 那条路现在会出事：bridge 由 launchd 的 KeepAlive 看着，
# 你杀掉一个它马上补一个，而脚本自己也起了一个 ⇒ 两个进程抢 9092。
#
# 新版只做一件事：让 launchd 重启这个 job。唯一正确的通道。
#
# 用法: ./restart.sh

set -u

LABEL="system/com.fatatalia.xiaoai-bridge"
PLIST="/Library/LaunchDaemons/com.fatatalia.xiaoai-bridge.plist"
BRIDGE_DIR="/Users/fatatalia/project/open-xiaoai-bridge"

# ── 1. 确认 launchd 里注册了这个 job ──
if ! launchctl print "$LABEL" >/dev/null 2>&1; then
    echo "❌ launchd 里找不到 $LABEL"
    echo "   检查 plist 是否存在: $PLIST"
    echo "   手动加载: sudo launchctl load -w $PLIST"
    exit 1
fi

# ── 2. 记录重启前的 PID，用来确认真的换了进程 ──
OLD_PID=$(launchctl print "$LABEL" 2>/dev/null | awk '$1 == "pid" && $2 == "=" {print $3; exit}')

# ── 3. 重启。kickstart -k = 先 SIGTERM 停掉，再拉起 ──
echo ">> 重启 $LABEL (旧 PID: ${OLD_PID:-无}) ..."
if ! launchctl kickstart -k "$LABEL" 2>/dev/null; then
    echo "❌ kickstart 失败"
    exit 1
fi

# ── 4. 等新进程出现（最多 15 秒）──
for _ in $(seq 1 15); do
    sleep 1
    NEW_PID=$(launchctl print "$LABEL" 2>/dev/null | awk '$1 == "pid" && $2 == "=" {print $3; exit}')
    if [ -n "${NEW_PID:-}" ] && [ "$NEW_PID" != "${OLD_PID:-}" ]; then
        echo ">> 已重启，新 PID: $NEW_PID"
        # 顺手确认 9092 端口回来了（API Server 是 restart 的主要用途）
        for _ in $(seq 1 10); do
            if lsof -ti :9092 >/dev/null 2>&1; then
                echo ">> 9092 端口已监听"
                exit 0
            fi
            sleep 1
        done
        echo "⚠️  进程起来了，但 9092 还没监听（可能仍在初始化，或 API_SERVER_ENABLE 没开）"
        exit 0
    fi
done

# ── 5. 超时 ──
echo "⚠️  15 秒内没检测到新 PID"
echo "   看日志: $BRIDGE_DIR/log/api.log"
launchctl print "$LABEL" 2>/dev/null | grep -E "state = |pid = |last exit" | sed 's/^/   /'
exit 1

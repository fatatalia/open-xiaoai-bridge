#!/bin/bash
# XiaoAI Bridge 停止脚本
#
# 2026-10-28 重写。
# 旧版 kill -9 掉进程。但 bridge 由 launchd 的 KeepAlive 看着，
# 杀掉它三秒后就会自己回来 —— 「停止」形同虚设。
# 要真停，必须让 launchd 放手：bootout 卸载 job。
#
# 用法: ./stop.sh        （停止，不自动恢复）
#       ./start.sh       （恢复）
#
# 注：bootout 之后 launchd 不再拉起，直到下次 bootstrap / 重启机器
#     （plist 里有 RunAtLoad，开机/登录会重新载入）。

set -u

LABEL="system/com.fatatalia.xiaoai-bridge"

# ── 本来就没加载 ──
if ! launchctl print "$LABEL" >/dev/null 2>&1; then
    echo ">> $LABEL 未加载，无需停止"
    exit 0
fi

PID=$(launchctl print "$LABEL" 2>/dev/null | awk '$1 == "pid" && $2 == "=" {print $3; exit}')
echo ">> 卸载 $LABEL (当前 PID: ${PID:-无}) ..."

# bootout 会先 SIGTERM 停进程，再摘掉 job；进程若卡住会被 SIGKILL
if ! launchctl bootout "$LABEL" 2>/dev/null; then
    echo "❌ bootout 失败"
    exit 1
fi

# ── 确认真的没了 ──
for _ in $(seq 1 10); do
    sleep 1
    if ! launchctl print "$LABEL" >/dev/null 2>&1; then
        echo ">> 已停止（job 已从 launchd 卸载）"
        # 端口也应释放
        for _ in $(seq 1 5); do
            if ! lsof -ti :9092 >/dev/null 2>&1; then
                echo ">> 9092 端口已释放"
                echo ">> 恢复用: ./start.sh"
                exit 0
            fi
            sleep 1
        done
        echo "⚠️  job 已卸载，但 9092 仍被占用（可能有残留进程）:"
        lsof -ti :9092 2>/dev/null | sed 's/^/   PID /'
        exit 0
    fi
done

echo "⚠️  bootout 后 job 仍存在，手动检查:"
launchctl print "$LABEL" 2>/dev/null | grep -E "state = |pid = " | sed 's/^/   /'
exit 1

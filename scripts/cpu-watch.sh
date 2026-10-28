#!/bin/bash
# cpu-watch.sh — 定时采样 open-xiaoai-bridge 的 CPU / 内存 / 线程数
# 目的：观察 CPU 是否随时间逐步升高（历史问题）
# 记录两个口径：
#   cpu_avg   = 累计 CPU 时间 / 运行时长  ← 真实平均值，用于看趋势
#   cpu_inst  = ps 的瞬时采样值          ← 会剧烈波动，仅参考
# 由 launchd com.fatatalia.xiaoai-cpu-watch 每 5 分钟调用

BRIDGE="/Users/fatatalia/project/open-xiaoai-bridge"
CSV="$BRIDGE/log/cpu-watch.csv"
ERR="$BRIDGE/log/cpu-watch.err"

# 表头
if [ ! -f "$CSV" ]; then
    echo "ts,pid,cpu_avg_pct,cpu_inst_pct,rss_kb,threads,uptime" > "$CSV"
fi

PID=$(lsof -ti :9092 2>/dev/null | head -1)
if [ -z "$PID" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S'),-,-,-,-,-,NOT_RUNNING" >> "$CSV"
    exit 0
fi

INFO=$(ps -p "$PID" -o time=,etime=,rss=,pcpu= 2>/dev/null)
if [ -z "$INFO" ]; then
    exit 0
fi

CPUTIME=$(echo "$INFO" | awk '{print $1}')
ETIME=$(echo "$INFO" | awk '{print $2}')
RSS=$(echo "$INFO" | awk '{print $3}')
INST=$(echo "$INFO" | awk '{print $4}')
NTHR=$(ps -M -p "$PID" 2>/dev/null | tail -n +2 | wc -l | tr -d ' ')

# CPU 累计时间 -> 秒（格式 MM:SS.SS 或 HH:MM:SS.SS）
SECS=$(echo "$CPUTIME" | awk -F: '{if (NF==3) printf "%.0f", $1*3600+$2*60+$3; else if (NF==2) printf "%.0f", $1*60+$2; else printf "%.0f", $1}')

# 运行时长 -> 秒（格式 [[DD-]HH:]MM:SS）
ESECS=$(echo "$ETIME" | awk -F'[-:]' '{
    if (NF==4) printf "%.0f", $1*86400+$2*3600+$3*60+$4;
    else if (NF==3) printf "%.0f", $1*3600+$2*60+$3;
    else if (NF==2) printf "%.0f", $1*60+$2;
    else printf "%.0f", $1
}')

AVG=$(awk -v c="$SECS" -v e="$ESECS" 'BEGIN { if (e > 0) printf "%.1f", c*100/e; else print "0.0" }')

echo "$(date '+%Y-%m-%d %H:%M:%S'),$PID,$AVG,$INST,$RSS,$NTHR,$ETIME" >> "$CSV"

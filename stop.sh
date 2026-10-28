#!/bin/bash
# 1. 定义程序特征名（用于唯一识别进程）
APP_NAME="open-xiaoai-bridge"

# 2. 查找进程PID
#PID=$(ps -ef | grep "$APP_NAME" | grep -v grep | awk '{print $2}')
PID=$(pgrep -f "[o]pen-xiaoai-bridge.*python3.*main\.py")

# 3. 检查进程是否存在
if [ -z "$PID" ]; then
    echo ">> $APP_NAME 尚未运行."
else
    # 4. 杀死进程
    echo ">> 找到 $APP_NAME 进程 (PID: $PID), 正在停止..."
    kill -9 $PID
    echo ">> $APP_NAME 已停止."
fi

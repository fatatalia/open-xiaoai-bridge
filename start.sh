#!/bin/bash
PID=$(ps -ef | grep "open-xiaoai-bridge" | grep -v grep | awk '{print $2}')
if [ -z "$PID" ]; then
    echo "Starting Application..."
    nohup bash -c "API_SERVER_ENABLE=1 API_SERVER_HOST=0.0.0.0 DSH_ENABLE=1 LOGLEVEL=debug ./scripts/start.sh" > ./log/api.log 2>&1 &
    echo "Application started."
else
    echo "Application is already running (PID: $PID)."
fi

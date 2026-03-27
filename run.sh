#!/bin/bash

# 加载 .env 文件中的环境变量
set -a
source .env
set +a

# 运行同步脚本
python3 sync_calendar.py

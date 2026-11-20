@echo off
REM heartbeat_relay 常驻心跳启动器
REM 用法：注册为 Windows 计划任务（HeartbeatRelay），以 SYSTEM 权限、会话不灭常驻运行。
REM 默认每 60s 重读 queue.json，捡 pending 的题目跑共享黑板多智能体讨论。
python "%~dp0heartbeat_relay.py" serve --interval 60

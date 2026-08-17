#!/bin/bash
set -e

# Docker restart keeps the container filesystem.  Xvfb itself is gone, but its
# lock/socket may remain and make the next boot report "Server is already
# active for display 99".
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
mkdir -p /tmp/.X11-unix

# 启动虚拟显示
Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &
export DISPLAY=:99

# 等待 Xvfb 就绪
sleep 1

# 启动 x11vnc（无密码，仅本地 VNC）
if [ -n "$VNC_PASSWORD" ]; then
    x11vnc -display :99 -rfbauth <(x11vnc -storepasswd "$VNC_PASSWORD" /tmp/vncpass && echo /tmp/vncpass) -forever -shared &
else
    x11vnc -display :99 -nopw -forever -shared &
fi

# 启动 noVNC（端口 6080 -> VNC 5900）
websockify --web=/usr/share/novnc 6080 localhost:5900 &

# 启动 FastAPI 后端
exec uvicorn main:app --host 0.0.0.0 --port 8000

@echo off
title Vessel Dashboard Launcher

REM ── If ngrok.exe isn't on your PATH, set the full path here ──
REM Example: set NGROK_PATH=C:\ngrok\ngrok.exe
set NGROK_PATH=C:\Users\fansuri\Documents\pro\fyp\vessel-dashboard\ngrok\ngrok.exe

echo ============================================
echo   Starting Node dashboard server ...
echo ============================================
start "Dashboard Server" cmd /k "cd /d %~dp0 && npm start"

timeout /t 3 >nul

echo ============================================
echo   Starting ngrok tunnel (public URL) ...
echo   Check the ngrok window for your https://
echo   ...ngrok-free.app link to share.
echo ============================================
start "Ngrok Tunnel" cmd /k "%NGROK_PATH% http 8080"

timeout /t 2 >nul
start "" http://localhost:8080

echo ============================================
echo   Starting Python inference bridge ...
echo   (connects to ESP32 over WiFi, pushes to
echo    the dashboard server started above)
echo ============================================
start "Inference Bridge" cmd /k "cd /d %~dp0 && python main.py"

echo All processes launched in separate windows.
echo   - Dashboard Server : Node.js relay
echo   - Ngrok Tunnel      : look here for your public link
echo   - Inference Bridge  : Python + ESP32 connection
echo Close this window any time - it is not needed anymore.
pause
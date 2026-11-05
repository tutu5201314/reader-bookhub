@echo off
title bookhub 开关
cd /d "%~dp0"

rem 双击一次=启动服务，再双击=停止服务
rem 主端口 8787（网页台+API），80 空闲时额外给 Reader 网关用
rem 若改了 config.json 里的 port，请同步改下面的 PORT

set PORT=8787

netstat -ano | findstr "127.0.0.1:%PORT% " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 goto stop

:start
echo [bookhub] 正在启动...
start "bookhub" /min python server.py
set tries=0

:waitloop
ping -n 3 127.0.0.1 >nul
netstat -ano | findstr "127.0.0.1:%PORT% " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 goto up
set /a tries+=1
if %tries% lss 8 goto waitloop
echo [bookhub] 启动失败，请在命令行运行 python server.py 查看报错
goto end

:up
echo [bookhub] 已启动，网页台: http://127.0.0.1:%PORT%
echo [bookhub] Reader 网关地址请看启动日志打印
goto end

:stop
echo [bookhub] 正在停止...
set FOUND=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr "127.0.0.1:%PORT% " ^| findstr "LISTENING"') do (
  tasklist /FI "PID eq %%a" 2>nul | findstr /I "python" >nul
  if not errorlevel 1 (
    taskkill /PID %%a /F >nul 2>&1
    set FOUND=1
  )
)
if %FOUND%==0 echo [bookhub] 注意：%PORT% 被其他程序占用（非 python），请先释放该端口
echo [bookhub] 已停止。在线搜书暂不可用（已缓存章节和 txt 不受影响）
goto end

:end
ping -n 4 127.0.0.1 >nul

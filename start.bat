@echo off
cd /d "%~dp0"
REM 优先用 anaconda 绝对路径（系统 PATH 无 Python 时双击也能启动），找不到再退回 PATH 的 pythonw
set "PYW=E:\Program Files\anaconda3\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"
"%PYW%" main.py

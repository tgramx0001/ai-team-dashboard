@echo off
setlocal
echo ==============================================
echo 🐴 Setting up AI Team Workstation (Windows)
echo ==============================================

REM 1. Cek python
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Python tidak ditemukan! Silakan install Python 3 dari python.org dan centang "Add to PATH".
    pause
    exit /b 1
)

REM 2. Buat venv
if not exist venv (
    echo 📦 Membuat virtual environment (venv)...
    python -m venv venv
)

REM 3. Install requirements
echo 📥 Mengunduh pustaka dependensi...
call venv\Scripts\activate.bat
python -m pip install -q --upgrade pip
pip install -q -r requirements.txt

REM 4. Jalankan
echo ----------------------------------------------
echo ✅ Instalasi selesai!
echo 🚀 Menjalankan Workstation di http://localhost:8090 ...
echo Buka browser di: http://localhost:8090
echo ----------------------------------------------
python main.py
pause

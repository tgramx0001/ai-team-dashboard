#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "=============================================="
echo "🐴 Setting up AI Team Workstation (Linux/macOS)"
echo "=============================================="

# 1. Pastikan Python 3 tersedia
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 tidak ditemukan. Silakan pasang python3 terlebih dahulu."
    exit 1
fi

# 2. Buat virtual environment jika belum ada
if [ ! -d "venv" ]; then
    echo "📦 Membuat isolated Python virtual environment (venv)..."
    python3 -m venv venv
fi

# 3. Install dependensi
echo "📥 Mengunduh pustaka yang dibutuhkan..."
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

# 4. Jalankan Workstation
echo "----------------------------------------------"
echo "✅ Instalasi selesai!"
echo "🚀 Membuka server di: http://localhost:8090"
echo "----------------------------------------------"
./venv/bin/python3 main.py

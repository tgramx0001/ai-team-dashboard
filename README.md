# AI Team Workstation

> **Autonomous Multi-Agent Orchestrator & Project-Aware Workstation**  
> Workstation mandiri yang mengorkestrasi tim spesialis kecerdasan buatan (Architect, Coder, QA, Security, Researcher) untuk mengeksekusi tugas rekayasa perangkat lunak maupun akademik secara terstruktur, terintegrasi langsung dengan filesystem proyek, alur verifikasi mandiri (*Auto-Fix QA Loop*), asisten pemoles prompt (*AI Prompt Enhancer*), dan mesin inti **Hermes Agent**.

---

## Mengapa Proyek Ini Dibuat?

Sebagian besar framework multi-agent populer (seperti AutoGPT, Dify, Flowise, atau CrewAI Enterprise):
* **Sangat boros RAM**: Membutuhkan ekosistem Docker berat yang memakan 1.5 – 3.5 GB memori.
* **Obrolan bebas (*free-chat*) tanpa pagar**: Agen sering berputar-putar dalam percakapan tanpa menghasilkan file kode yang konkret.
* **Terisolasi dari proyek nyata**: Jarang terhubung langsung dengan struktur file codebase lokal atau aturan arsitektur (`AGENTS.md`).

**AI Team Workstation** hadir sebagai alternatif ultra-ringan:
1. **Konsumsi RAM <60 MB**: Dibangun hanya dengan **FastAPI (1 file Python `main.py`)** dan **Tailwind CSS CDN (1 file `index.html`)**. Tanpa Docker, tanpa Node.js/npm.
2. **Eksekusi Berpagar (SOP Pipeline)**: Setiap agen memiliki peran tunggal dengan input-output terstandar (ala MetaGPT / LangGraph Supervisor).
3. **Project-Awareness**: Membaca konteks proyek, memindai *codebase skeleton*, dan mampu menulis langsung blok multi-file ke direktori target dengan cadangan otomatis (`.bak`).
4. **Terhubung ke Hermes Core**: Mengakses langsung 81 SOP skill Hermes, riwayat percakapan (`state.db`), dan switcher model LLM tanpa perlu menjalankan dashboard web Hermes terpisah.

---

## Peta Fungsi & Panduan Antarmuka (UI Controls)

Berikut penjelasan rinci mengenai setiap tombol, opsi eksekusi, dan komponen yang ada di dashboard:

```
+---------------------------------------------------------------------------------------------------------+
| [AT Workstation]   [model: bai ▾]   [Sessions]                       [mobile-programming-2 ▾]           |
+---------------------------------------------------------------------------------------------------------+
| SIDEBAR KIRI (Settings & Skills)    | CENTRAL WORKSPACE (Task & Inspector)   | RIGHT DRAWER (CRUD Files)|
|                                     |                                        |                          |
| 1. Preset Mode (Auto/Dev/Audit)     | 1. Prompt Command Input Box (⌘+Enter)  | 1. Breadcrumb Navigation |
| 2. Execution Flags:                 | 2. AI Prompt Enhancer (Alt+E)          | 2. File Action (+File,+Dir)|
|    - Direct Multi-File Writer       | 3. Live Stage Timeline Tracker         | 3. Inline Rename (ren)   |
|    - Human Approval Gate            | 4. Tab Area:                           | 4. Delete with Modal(del)|
|    - Auto-Fix QA Loop (0x/1x/2x)    |    - [Final Deliverable] (Dokumen)     | 5. In-Browser Editor     |
|    - Save DELIVERABLE.md            |    - [Extracted Files] (Preview kode)  |                          |
| 3. Hermes Skills SOPs (81 Chips)    |    - [Agent Logs] (Raw console output) |                          |
+---------------------------------------------------------------------------------------------------------+
```

---

### 1. AI Prompt Enhancer (Alt+E)

* **Fungsi**: Membantu pengguna yang bingung merumuskan prompt tugas agar hasilnya presisi dan terstruktur.
* **Cara Pakai**:
  1. Tulis ide kasar atau instruksi singkat di kotak perintah (misal: *"bikin absensi qr flutter pake jwt"*).
  2. Klik tombol **`Enhance`** di sebelah kanan bawah kotak input (atau tekan shortcut **`Alt+E`**).
  3. Sistem otomatis menganalisis tech stack proyek dari `AGENTS.md` dan struktur codebase, lalu mengembangkan instruksi mentah tersebut menjadi spesifikasi teknis lengkap:
     - **Target/Goal**: Sasaran modul yang ingin dicapai.
     - **Komponen/Arsitektur**: Rincian file, model, controller, atau UI yang harus dibuat.
     - **Batasan Teknis & Konvensi**: Standar penulisan kode, penanganan error, dan efisiensi memori.
     - **Kriteria Keberhasilan (DoD)**: Skenario verifikasi yang harus lolos uji QA.

---

### 2. Opsi Eksekusi (Dispatch Settings)

| Opsi | Status Default | Penjelasan & Fungsi |
|---|---|---|
| **`Direct Multi-File Writer`** | **OFF (Mati)** | **Saklar penulisan otomatis ke disk.**<br>• **Jika OFF**: File kode hasil generate hanya ditampilkan di tab *Extracted Files* sebagai preview. Aman, tidak mengubah file asli Anda.<br>• **Jika ON**: Begitu task selesai, semua blok file `### FILE: ...` langsung ditulis/ditimpa ke direktori proyek target (otomatis membuat cadangan `.bak`). |
| **`Human Approval Gate`** | **OFF (Mati)** | **Rem tangan interaktif.**<br>Setelah peran perancang (*Architect* atau *Planner*) selesai menyusun spesifikasi, sistem **berhenti sementara (*pause*)**. Anda dapat meninjau rancangan konsepnya, memberi koreksi via teks masukan (*Human Feedback*), lalu menekan tombol **Approve & Lanjut** untuk meneruskan ke Coder. |
| **`Auto-Fix QA Loop`** | **1x (Maks 1 Siklus)** | **Siklus perbaikan bug mandiri.**<br>Jika agen QA menemukan kelemahan dan mengeluarkan vonis `VERDICT: NEEDS_REVISION`, sistem secara otomatis memicu peran *Lead Developer* untuk merevisi kode, lalu menguji ulang ke QA.<br>• Pilihan: `0x` (QA hanya melapor tanpa revisi), `1x` (1 kali revisi), `2x` (hingga 2 kali revisi). |
| **`Save DELIVERABLE.md`** | **ON (Aktif)** | Otomatis menyimpan dokumen deliverable akhir menjadi file Markdown berformat `DELIVERABLE_{task_id}.md` langsung di direktori kerja proyek Anda. |

---

### 3. Mode Pipeline (Preset)

* **`Auto (Dynamic Orchestrator - Default)`**:
  * Mengaktifkan **Supervisor Pattern**. Tahap 0 (*AI Team Orchestrator*) menganalisis prompt Anda dan secara cerdas menentukan tim spesialis yang dibutuhkan (misal: hanya butuh *Auditor* dan *QA*, atau butuh *Full Pipeline*).
* **`Dev (Software Engineering)`**:
  * Rantai sekuensial lengkap: `Architect` (desain API/skema) $\rightarrow$ `UI/UX` (layout/komponen) $\rightarrow$ `Coder` (implementasi kode produksi) $\rightarrow$ `QA` (verifikasi).
* **`Audit (Security Vulnerability & Patch)`**:
  * Khusus pengujian keamanan: `Security Auditor` (analisis celah OWASP/injeksi) $\rightarrow$ `Patch Developer` (buat perbaikan) $\rightarrow$ `Verification QA` (verifikasi hasil patch).
* **`Kuliah (Akademik & Riset Ilmiah)`**:
  * Khusus penyusunan karya tulis/makalah: `Researcher` (landasan teori/sitasi) $\rightarrow$ `Planner` (outline Bab 1–5) $\rightarrow$ `Writer` (isi pembahasan formal Bahasa Indonesia) $\rightarrow$ `Reviewer` (evaluasi rubrik).

---

### 4. Integrasi Hermes Core Engine

Workstation ini terintegrasi secara langsung dengan ekosistem **Hermes Agent**:

* **81 Hermes Skills SOPs (Chips di Sidebar)**:
  * Memindai direktori `~/.hermes/skills/` secara live (kategori: `creative`, `software-development`, `security`, `productivity`, `research`, dll.).
  * Mengklik chip skill (misalnya `popular-web-designs`, `qa-engineer`, `vibe-coding`, `systematic-debugging`) akan menyuntikkan dokumen SOP resmi skill tersebut ke dalam prompt agen.
* **`Model Switcher` (Navbar Atas)**:
  * Menampilkan model aktif (misal `bai`, `deepseek`, dll.).
  * Mengklik tombol ini membuka modal pemilih model yang terhubung ke 9Router / LLM gateway.
  * Mengubah model langsung memperbarui konfigurasi `model.default` di `~/.hermes/config.yaml`.
* **`Sessions Explorer` (Navbar Atas)**:
  * Membaca basis data SQLite `~/.hermes/state.db` secara langsung dalam mode *read-only*.
  * Menampilkan daftar sesi percakapan Telegram, Web, maupun Cron.
  * Anda bisa mengklik sesi mana pun untuk menginspeksi riwayat chat dan tool call langsung di browser.

---

### 5. Project Workspace & CRUD File Explorer (Drawer Kanan)

* **Working Directory Picker**:
  * Menentukan direktori proyek aktif tempat agen membaca kode dan menyimpan file.
  * Tersedia shortcut cepat untuk proyek lokal (`ai-team-hub`, `mobile-prog-2`, `sql-mystery`, `Kuliah Vault`, atau custom path).
* **In-Browser File Manager**:
  * **Navigasi Folder**: Breadcrumb path interaktif untuk masuk/keluar subdirektori.
  * **Operasi File**: Buat file baru (`+ File`), buat folder baru (`+ Dir`), ganti nama file (`ren`), dan hapus file (`del`).
  * **File Editor**: Klik file teks mana saja untuk membaca, menyunting, dan menyimpan (`Simpan`) langsung ke disk tanpa membuka terminal.
* **Aturan Proyek (`AGENTS.md`)**:
  * File khusus di root proyek yang berisi instruksi tech stack, gaya coding, dan batasan arsitektur. Otomatis dibaca dan disuntikkan ke konteks setiap agen.

---

### 6. Area Inspeksi Hasil (Inspector Tabs)

* **Timeline Cards**: Diagram visual tiap tahapan agen, status eksekusi, latensi durasi, dan badge error deskriptif.
* **Final Deliverable**: Tampilan kompilasi laporan akhir markdown dengan tombol salin satu klik.
* **Extracted Files**: Kartu preview file kode hasil ekstraksi tag `### FILE: path/ext`. Dilengkapi tombol salin per file dan tombol **`Write to Workspace`** untuk menerapkan kode ke proyek.
* **Agent Logs**: Log mentah percakapan dan respons tiap agen untuk kebutuhan *debugging* teknis.

---

## Panduan Instalasi (Super Gampang)

Aplikasi ini **100% portabel** dan dapat dijalankan di Windows, macOS, Linux, maupun VPS tanpa konfigurasi yang rumit.

### Opsi 1: Otomatis 1-Klik (Paling Direkomendasikan)

Cukup clone repo dan jalankan skrip instalasi otomatis:

* **Linux / macOS**:
  ```bash
  chmod +x setup.sh
  ./setup.sh
  ```
  *(Skrip akan otomatis membuat virtualenv `venv`, memasang seluruh dependensi, dan menyalakan server)*.

* **Windows**:
  Cukup **klik dua kali** file `setup.bat` (atau jalankan `setup.bat` via Command Prompt / PowerShell).

Buka browser di: **`http://localhost:8090`**

---

### Opsi 2: Instalasi Manual (Hanya 2 Baris)

Jika ingin memasang manual tanpa skrip:
```bash
# 1. Pasang dependensi
pip install -r requirements.txt

# 2. Jalankan
python3 main.py
```

---

### Menghubungkan ke Model AI (LLM Provider)

Workstation otomatis mendukung semua provider berstandar OpenAI API:

#### A. Menggunakan Ollama Lokal (Gratis & Offline 100%)
```bash
# 1. Jalankan model di terminal
ollama run qwen2.5-coder:7b

# 2. Jalankan workstation (Linux/Mac)
export LLM_BASE_URL="http://localhost:11434/v1"
export LLM_MODEL="qwen2.5-coder:7b"
python3 main.py
```
*(Pengguna Windows PowerShell: `$env:LLM_BASE_URL="http://localhost:11434/v1"`, `$env:LLM_MODEL="qwen2.5-coder:7b"`)*

#### B. Menggunakan LM Studio
1. Buka LM Studio $\rightarrow$ Tab **Developer / Local Server** $\rightarrow$ Klik **Start Server** (Port `1234`).
2. Jalankan workstation:
   ```bash
   export LLM_BASE_URL="http://localhost:1234/v1"
   export LLM_MODEL="deepseek-r1-distill-qwen-7b"
   python3 main.py
   ```

#### C. Menggunakan 9Router VPS via Tailscale
Jika Anda ingin komputer lokal memakai model dan API key dari VPS:
```bash
export LLM_BASE_URL="http://100.104.131.60:20128/v1"
export LLM_MODEL="bai"
python3 main.py
```

#### D. Menggunakan Cloud Provider (OpenAI / OpenRouter / Groq)
```bash
export LLM_BASE_URL="https://api.openai.com/v1"
export LLM_MODEL="gpt-4o-mini"
export LLM_API_KEY="sk-..."
python3 main.py
```

---

## Manajemen Layanan di VPS

Di VPS produksi, dashboard dikelola oleh *systemd user unit* dengan batas konsumsi memori 300 MB:
* **Service File**: `~/.config/systemd/user/ai-team.service`
* **Port**: `8090` (diakses via IP Tailscale: `http://100.104.131.60:8090`)

```bash
# Cek status layanan
systemctl --user status ai-team.service

# Restart layanan
systemctl --user restart ai-team.service

# Pantau log secara realtime
journalctl --user -u ai-team.service -f
```

---

## Proteksi Keamanan Bawaan
1. **Pagar Path Traversal**: Semua operasi baca/tulis/hapus file divalidasi dengan `Path.resolve().is_relative_to(root)`. Permintaan file yang mengarah ke luar folder proyek ditolak (`400 Bad Request`).
2. **Auto-Backup Aman (`.bak`)**: Penulisan file kode yang sudah ada secara otomatis membuat cadangan `.bak` di direktori yang sama sebelum file ditimpa.
3. **Tanpa Web Shell Bebas**: Antarmuka tidak menyediakan terminal shell interaktif sembarangan untuk mencegah celah *Remote Code Execution* (RCE).

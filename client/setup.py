import os
import sys
import json
import sqlite3
import winreg
import subprocess
import shutil
from colorama import init, Fore, Style

# Kütüphaneleri kur
def install_requirements():
    print("[*] Sistem kütüphaneleri optimize ediliyor...")
    packages = ["colorama", "cryptography>=42", "websockets>=15", "win10toast"]
    for package in packages:
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", package, "--quiet"])
        except Exception:
            print(f"[-] HATA: '{package}' kurulamadı. İnternet bağlantını kontrol et.")
            sys.exit(1)

install_requirements()
init(autoreset=True)

def print_sys(msg): print(Fore.GREEN + msg)

APP_DIR = os.path.join(os.environ.get('LOCALAPPDATA'), 'Yakishi', 'Terminal_Chat')

def setup_environment():
    if not os.path.exists(APP_DIR):
        os.makedirs(APP_DIR)
    
    # Dosyaları kopyala
    current_dir = os.path.dirname(os.path.abspath(__file__))
    for file_name in ["cli.py", "core.py"]:
        src = os.path.join(current_dir, file_name)
        dst = os.path.join(APP_DIR, file_name)
        if os.path.exists(src):
            shutil.copy2(src, dst)

    # Veritabanı
    conn = sqlite3.connect(os.path.join(APP_DIR, 'history.db'))
    conn.execute('''CREATE TABLE IF NOT EXISTS messages 
                  (id INTEGER PRIMARY KEY AUTOINCREMENT, room_hash TEXT, sender TEXT, content TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    # Aynı şifreli paket iki kez kaydedilemez (tekrar oynatma koruması)
    try:
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS ux_messages ON messages(room_hash, content)')
    except sqlite3.DatabaseError:
        pass
    conn.commit()
    conn.close()

    # Kişi Listesi (Contacts): ilk kişi eklendiğinde cli.py tarafından
    # Windows DPAPI ile şifrelenmiş olarak oluşturulur (contacts.dat).

    # Ayarlar
    print_sys("--- YAKISHI AĞI KURULUMU ---")
    username = input(Fore.GREEN + "Kullanıcı Adın: " + Style.RESET_ALL).strip()[:20] or "Anonim"
    color = "GREEN" # Varsayılan renk, cli içinden değiştirilebilir
    with open(os.path.join(APP_DIR, 'config.json'), 'w', encoding='utf-8') as f:
        json.dump({"username": username, "color": color}, f, indent=4)

def register_background_service():
    # Windows Başlangıcına Ekle (Sessiz Çalışma)
    try:
        pythonw_path = sys.executable.replace("python.exe", "pythonw.exe")
        core_script = os.path.join(APP_DIR, "core.py")
        run_command = f'"{pythonw_path}" "{core_script}"'
        
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run', 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, 'YakishiCore', 0, winreg.REG_SZ, run_command)
        winreg.CloseKey(key)
        print_sys("[+] Yakishi Core hayalet servisi başlangıca eklendi.")
        
        # Servisi hemen şimdi başlat (Penceresiz)
        subprocess.Popen([pythonw_path, core_script], creationflags=subprocess.CREATE_NO_WINDOW)
        print_sys("[+] Arka plan servisi başlatıldı.")
    except Exception as e:
        print(Fore.RED + f"[-] Servis kaydı başarısız: {e}")

def create_launcher():
    # PATH'e ekle ve BAT oluştur
    bat_path = os.path.join(APP_DIR, 'TerminalChat.bat')
    with open(bat_path, 'w') as f:
        f.write("@echo off\n")
        f.write(f'python "{os.path.join(APP_DIR, "cli.py")}" %*\n')
    
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Environment', 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE)
        current_path, _ = winreg.QueryValueEx(key, 'PATH')
        if APP_DIR not in current_path:
            winreg.SetValueEx(key, 'PATH', 0, winreg.REG_EXPAND_SZ, current_path + f";{APP_DIR}")
        winreg.CloseKey(key)
    except: pass

if __name__ == "__main__":
    os.system('cls')
    setup_environment()
    create_launcher()
    register_background_service()
    print_sys("\n[✓] Kurulum Tamamlandı! Artık herhangi bir terminalde 'TerminalChat' yazarak girebilirsin.")
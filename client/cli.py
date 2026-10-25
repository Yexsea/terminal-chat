"""Yakishi Terminal Chat — chat window (CLI)."""
import getpass
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time

from colorama import Fore, Style, init

init(autoreset=True)

APP_DIR = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'Yakishi', 'Terminal_Chat')
CONFIG_PATH = os.path.join(APP_DIR, 'config.json')
CONTACTS_PATH = os.path.join(APP_DIR, 'contacts.dat')        # DPAPI ile şifreli
OLD_CONTACTS_PATH = os.path.join(APP_DIR, 'contacts.json')   # eski sürüm (düz metin)
TOKEN_PATH = os.path.join(APP_DIR, 'ipc.token')
IPC_HOST = '127.0.0.1'
IPC_PORT = int(os.environ.get('YAKISHI_IPC_PORT', 65432))
MIN_PASSWORD = 12
COLORS = ["RED", "GREEN", "YELLOW", "BLUE", "CYAN", "MAGENTA"]

_UNSAFE = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]')


def clean(value, limit=2000):
    """Karşı taraftan gelen metindeki terminal kaçış kodlarını ve kontrol karakterlerini atar."""
    if not isinstance(value, str):
        return ''
    return _UNSAFE.sub('', value).strip()[:limit]


# ---------------------------------------------------------------- Windows DPAPI
# Rehberdeki ortak şifreler, Windows'un kullanıcı hesabına bağlı şifrelemesiyle saklanır.
# Başka bir kullanıcı ya da bilgisayar dosyayı açamaz.
def _dpapi(data, protect):
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = Blob()
    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(blob_in), "Yakishi Terminal Chat", None, None, None,
                                      CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None,
                                        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def load_contacts():
    if os.path.exists(CONTACTS_PATH):
        try:
            with open(CONTACTS_PATH, 'rb') as f:
                return json.loads(_dpapi(f.read(), protect=False).decode('utf-8'))
        except Exception:
            print(Fore.RED + "[-] Rehber açılamadı (başka bir kullanıcıya ait olabilir).")
            return {}
    # Eski sürümden kalan düz metin rehberi şifreli hale getir
    if os.path.exists(OLD_CONTACTS_PATH):
        try:
            with open(OLD_CONTACTS_PATH, 'r', encoding='utf-8') as f:
                contacts = json.load(f)
            save_contacts(contacts)
            os.remove(OLD_CONTACTS_PATH)
            return contacts
        except Exception:
            return {}
    return {}


def save_contacts(contacts):
    data = _dpapi(json.dumps(contacts).encode('utf-8'), protect=True)
    tmp = CONTACTS_PATH + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, CONTACTS_PATH)


class YakishiCLI:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.config = self.load_json(CONFIG_PATH)
        self.config["username"] = clean(self.config.get("username", ""), 20) or "Anonim"
        if self.config.get("color") not in COLORS:
            self.config["color"] = "GREEN"
        self.contacts = load_contacts()

    def load_json(self, path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save_json(self, data, path):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)

    def send(self, packet):
        self.sock.sendall((json.dumps(packet) + "\n").encode('utf-8'))

    def start_core_service(self):
        # Arka plan servisi çalışmıyorsa pythonw.exe ile penceresiz (hayalet) olarak başlatır
        try:
            core_script = os.path.join(APP_DIR, "core.py")
            pythonw_path = sys.executable.replace("python.exe", "pythonw.exe")
            subprocess.Popen([pythonw_path, core_script], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass

    def connect_core(self):
        dots = ["   ", ".  ", ".. ", "..."]
        i = 0
        attempts = 0
        while True:
            try:
                self.sock.connect((IPC_HOST, IPC_PORT))
                with open(TOKEN_PATH, 'r', encoding='utf-8') as f:
                    token = f.read().strip()
                # Core sadece bu gizli belirteci bilen pencereyle konuşur
                self.send({"type": "hello", "token": token})
                sys.stdout.write("\r" + " " * 30 + "\r")
                sys.stdout.flush()
                break
            except (ConnectionRefusedError, FileNotFoundError):
                attempts += 1
                # Eğer ilk denemede bağlanamazsa, Core muhtemelen kapalıdır. Hemen uyandır!
                if attempts == 2:
                    self.start_core_service()
                sys.stdout.write(Fore.GREEN + f"\rCore servis aranıyor{dots[i % 4]}")
                sys.stdout.flush()
                i += 1
                time.sleep(0.15)

    def listen(self):
        buffer = ""
        while True:
            try:
                data = self.sock.recv(4096).decode('utf-8', errors='ignore')
                if not data:
                    break
                buffer += data
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    p = json.loads(line)
                    if p.get("type") in ("chat", "history"):
                        sender, content = clean(p.get("sender"), 20), clean(p.get("content"))
                        if sender == self.config["username"]:
                            color = getattr(Fore, self.config["color"], Fore.WHITE)
                        else:
                            color = getattr(Fore, p.get("color"), Fore.WHITE) if p.get("color") in COLORS else Fore.WHITE
                        prefix = "[GEÇMİŞ] " if p["type"] == "history" else ""
                        print(f"{color}{Style.BRIGHT}{prefix}{sender}: {Style.NORMAL}{content}")
                    elif p.get("type") == "system":
                        print(Fore.GREEN + f"[SİSTEM] {clean(p.get('content'))}")
            except Exception:
                break

    def main_menu(self):
        os.system('cls' if os.name == 'nt' else 'clear')
        print(Fore.GREEN + Style.BRIGHT + "=== YAKISHI TERMINAL ===")
        print("1. Rehber (Sohbet Başlat)")
        print("2. Yeni Kişi/Oda Ekle")
        print("3. Profil Ayarları")
        print("4. Çıkış")

        choice = input("\nSeçim: ")
        if choice == "1":
            self.show_contacts()
        elif choice == "2":
            self.add_contact()
        elif choice == "3":
            self.settings_menu()
        elif choice == "4":
            sys.exit()

    def show_contacts(self):
        os.system('cls' if os.name == 'nt' else 'clear')
        print(Fore.GREEN + "--- REHBER ---")
        if not self.contacts:
            print(Fore.YELLOW + "Henüz kimse ekli değil.")
            time.sleep(2)
            return

        names = list(self.contacts.keys())
        for idx, name in enumerate(names, 1):
            print(f"{idx}. {name}")

        choice = input("\nKiminle konuşmak istiyorsun? (Numara/İptal): ")
        if choice.isdigit() and 1 <= int(choice) <= len(names):
            contact_name = names[int(choice) - 1]
            self.enter_chat(contact_name, self.contacts[contact_name])

    def add_contact(self):
        print(Fore.GREEN + "\n--- YENİ KİŞİ ---")
        name = clean(input("Kişi/Oda Adı: "), 40)
        if not name:
            return
        print(f"Ortak şifre en az {MIN_PASSWORD} karakter olmalı.")
        print("Boş bırakırsan senin için güçlü bir şifre üretirim; onu arkadaşına başka bir yoldan ilet.")
        password = getpass.getpass("Ortak Şifre (yazarken görünmez): ").strip()
        if not password:
            password = secrets.token_urlsafe(18)
            print(Fore.YELLOW + f"\nÜretilen şifre: {password}")
            print(Fore.YELLOW + "Arkadaşın da bu kişiyi aynı şifreyle eklemeli.")
            input("Devam etmek için Enter...")
        elif len(password) < MIN_PASSWORD:
            print(Fore.RED + f"Şifre çok kısa. En az {MIN_PASSWORD} karakter olmalı.")
            time.sleep(2)
            return
        self.contacts[name] = password
        save_contacts(self.contacts)
        print(Fore.GREEN + "Kişi eklendi!")
        time.sleep(1)

    def settings_menu(self):
        print(Fore.GREEN + "\n--- AYARLAR ---")
        new_name = clean(input(f"Yeni İsim (Mevcut: {self.config['username']}): "), 20)
        self.config["username"] = new_name or self.config["username"]
        print("Renkler: " + ", ".join(COLORS))
        new_color = input(f"Yeni Renk (Mevcut: {self.config['color']}): ").strip().upper()
        if new_color in COLORS:
            self.config["color"] = new_color
        self.save_json(self.config, CONFIG_PATH)
        print(Fore.GREEN + "Profil güncellendi.")
        time.sleep(1)

    def enter_chat(self, name, password):
        os.system('cls' if os.name == 'nt' else 'clear')
        print(Fore.GREEN + f">>> {name} ile bağlantı kuruluyor... (-exit ile çıkabilirsin)\n")
        self.send({"type": "auth", "password": password})

        threading.Thread(target=self.listen, daemon=True).start()

        while True:
            try:
                msg = input()
                if msg == "-exit":
                    self.sock.close()
                    sys.exit()
                msg = clean(msg)
                if msg:
                    sys.stdout.write("\033[F\033[K")
                    my_color = getattr(Fore, self.config["color"], Fore.WHITE)
                    print(f"{my_color}{Style.BRIGHT}{self.config['username']}: {Style.NORMAL}{msg}")
                    self.send({"type": "chat", "content": msg})
            except KeyboardInterrupt:
                sys.exit()

    def run(self):
        # 1. Aşama: Core'a bağlan (Yoksa uyandır)
        self.connect_core()

        # 2. Aşama: Ana menü döngüsü
        while True:
            self.main_menu()


if __name__ == "__main__":
    YakishiCLI().run()

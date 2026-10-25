"""Yakishi Terminal Chat — background service (core).

Holds the relay connection (WebSocket over TLS), encrypts and decrypts messages, keeps the encrypted
history and shows a notification when a message arrives while no chat window
is open. The chat window talks to it over an authenticated local socket.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import socket
import sqlite3
import threading
import time

from cryptography.fernet import Fernet, InvalidToken
from websockets.sync.client import connect

try:
    from win10toast import ToastNotifier
except Exception:                         # bildirimler isteğe bağlı
    ToastNotifier = None

APP_DIR = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'Yakishi', 'Terminal_Chat')
CONFIG_PATH = os.path.join(APP_DIR, 'config.json')
DB_PATH = os.path.join(APP_DIR, 'history.db')
TOKEN_PATH = os.path.join(APP_DIR, 'ipc.token')

RELAY_URL = os.environ.get('YAKISHI_RELAY', 'wss://terminal-chat-t08h.onrender.com')
IPC_HOST = '127.0.0.1'                    # Güvenlik: sadece bu bilgisayara açık
IPC_PORT = int(os.environ.get('YAKISHI_IPC_PORT', 65432))

KDF_SALT = b'yakishi-terminal-chat/v2'
MESSAGE_TTL = 10 * 60                     # 10 dakikadan eski paketler reddedilir (tekrar oynatma koruması)
MAX_TEXT = 2000
MAX_LINE = 64 * 1024
COLORS = {"RED", "GREEN", "YELLOW", "BLUE", "CYAN", "MAGENTA", "WHITE"}

# Terminal kaçış kodları, kontrol karakterleri ve yön değiştiren Unicode karakterleri
_UNSAFE = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]')


def clean(value, limit):
    if not isinstance(value, str):
        return ''
    return _UNSAFE.sub('', value).strip()[:limit]


def derive_session(password):
    """Yavaş ve tuzlu bir anahtar türetme (scrypt) ile şifreleme anahtarı ve oda kimliği üretir.

    Oda kimliği de aynı yavaş türetmeden geldiği için, röleyi izleyen biri parolayı
    hızlıca deneme yanılma ile bulamaz.
    """
    raw = hashlib.scrypt(password.encode('utf-8'), salt=KDF_SALT, n=2 ** 15, r=8, p=1,
                         dklen=64, maxmem=64 * 1024 * 1024)
    cipher = Fernet(base64.urlsafe_b64encode(raw[:32]))
    room_id = hashlib.sha256(b'room-id' + raw[32:]).hexdigest()
    return cipher, room_id


class YakishiCore:
    def __init__(self):
        os.makedirs(APP_DIR, exist_ok=True)
        self.cipher = None
        self.room_hash = None
        self.cli_conn = None
        self.cli_lock = threading.Lock()
        self.ws = None
        self.ws_lock = threading.Lock()
        self.toaster = ToastNotifier() if ToastNotifier else None
        self.running = True
        self.ipc_token = secrets.token_hex(32)
        self.init_db()

    # ---------------------------------------------------------------- storage
    def load_config(self):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        name = clean(cfg.get('username'), 20) or 'Anonim'
        color = cfg.get('color') if cfg.get('color') in COLORS else 'GREEN'
        return name, color

    def db(self):
        return sqlite3.connect(DB_PATH)

    def init_db(self):
        with self.db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS messages
                            (id INTEGER PRIMARY KEY AUTOINCREMENT, room_hash TEXT, sender TEXT,
                             content TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
            try:
                conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS ux_messages ON messages(room_hash, content)')
            except sqlite3.DatabaseError:
                pass

    def store(self, token):
        """Şifreli paketi geçmişe yazar. Aynı paket daha önce görüldüyse False döner."""
        with self.db() as conn:
            seen = conn.execute('SELECT 1 FROM messages WHERE room_hash = ? AND content = ?',
                                (self.room_hash, token)).fetchone()
            if seen:
                return False
            conn.execute('INSERT INTO messages (room_hash, sender, content) VALUES (?, ?, ?)',
                         (self.room_hash, '', token))
        return True

    def history(self):
        with self.db() as conn:
            rows = conn.execute('SELECT content FROM messages WHERE room_hash = ? ORDER BY id ASC',
                                (self.room_hash,)).fetchall()
        for (token,) in rows:
            body = self.open_token(token, ttl=None)
            if body:
                yield body

    # ---------------------------------------------------------------- crypto
    def seal(self, text):
        name, color = self.load_config()
        body = {"sender": name, "color": color, "text": clean(text, MAX_TEXT), "n": secrets.token_hex(8)}
        return self.cipher.encrypt(json.dumps(body).encode('utf-8')).decode('ascii')

    def open_token(self, token, ttl=MESSAGE_TTL):
        try:
            data = self.cipher.decrypt(token.encode('ascii'), ttl=ttl)
            body = json.loads(data.decode('utf-8'))
        except (InvalidToken, ValueError, UnicodeError, AttributeError):
            return None
        if not isinstance(body, dict):
            return None
        text = clean(body.get('text'), MAX_TEXT)
        if not text:
            return None
        color = body.get('color') if body.get('color') in COLORS else 'WHITE'
        return {"sender": clean(body.get('sender'), 20) or '?', "color": color, "content": text}

    # ---------------------------------------------------------------- chat window (IPC)
    def send_to_cli(self, data):
        with self.cli_lock:
            if not self.cli_conn:
                return False
            try:
                self.cli_conn.sendall((json.dumps(data) + "\n").encode('utf-8'))
                return True
            except OSError:
                self.cli_conn = None
                return False

    def open_ipc(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            # Windows: başka bir program aynı portu ele geçiremesin
            server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((IPC_HOST, IPC_PORT))   # ikinci bir core burada hata verip kapanır
        server.listen(2)
        # Belirteç sadece port gerçekten alındıktan sonra yazılır,
        # böylece yanlışlıkla açılan ikinci bir core çalışanın belirtecini ezemez.
        with open(TOKEN_PATH, 'w', encoding='utf-8') as f:
            f.write(self.ipc_token)
        return server

    def serve_ipc(self, server):
        while self.running:
            conn, _ = server.accept()
            threading.Thread(target=self.manage_cli, args=(conn,), daemon=True).start()

    def manage_cli(self, conn):
        buffer = ""
        authed = False
        conn.settimeout(10)                   # tanışma 10 sn içinde olmalı
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buffer += data.decode('utf-8', errors='ignore')
                if len(buffer) > MAX_LINE:
                    break
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    try:
                        packet = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(packet, dict):
                        continue

                    if not authed:
                        # İlk satır, core'un dosyaya yazdığı gizli belirteç olmalı.
                        token = packet.get("token")
                        if packet.get("type") != "hello" or not isinstance(token, str) \
                                or not secrets.compare_digest(token, self.ipc_token):
                            return
                        authed = True
                        conn.settimeout(None)
                        with self.cli_lock:
                            if self.cli_conn and self.cli_conn is not conn:
                                try:
                                    self.cli_conn.close()
                                except OSError:
                                    pass
                            self.cli_conn = conn
                        continue

                    if packet.get("type") == "auth" and isinstance(packet.get("password"), str):
                        self.cipher, self.room_hash = derive_session(packet["password"])
                        self.join_room()
                        for body in self.history():
                            self.send_to_cli({"type": "history", **body})
                        self.send_to_cli({"type": "system", "content": "Oda bağlantısı kuruldu."})

                    elif packet.get("type") == "chat" and self.cipher:
                        text = clean(packet.get("content"), MAX_TEXT)
                        if not text:
                            continue
                        token = self.seal(text)
                        if self.relay_send({"type": "msg", "payload": token}):
                            self.store(token)
                        else:
                            self.send_to_cli({"type": "system", "content": "Bağlantı yok, mesaj gönderilemedi."})
        except (OSError, socket.timeout):
            pass
        finally:
            with self.cli_lock:
                if self.cli_conn is conn:
                    self.cli_conn = None
            try:
                conn.close()
            except OSError:
                pass

    # ---------------------------------------------------------------- relay
    def relay_send(self, packet):
        with self.ws_lock:
            if not self.ws:
                return False
            try:
                self.ws.send(json.dumps(packet))
                return True
            except Exception:
                return False

    def join_room(self):
        if self.room_hash:
            self.relay_send({"type": "join", "room": self.room_hash})

    def on_packet(self, raw):
        if not isinstance(raw, str) or len(raw) > MAX_LINE:
            return
        try:
            packet = json.loads(raw)
        except ValueError:
            return
        if not isinstance(packet, dict) or packet.get("type") != "msg" or not self.cipher:
            return
        token = packet.get("payload")
        if not isinstance(token, str):
            return
        body = self.open_token(token)          # yanlış odadan, bozuk ya da eski paket → None
        if not body or not self.store(token):   # store False → daha önce görülmüş (tekrar)
            return
        if not self.send_to_cli({"type": "chat", **body}) and self.toaster:
            self.toaster.show_toast("Yakishi Chat", f"{body['sender']}: Yeni mesaj", duration=5, threaded=True)

    def handle_remote(self):
        delay = 2
        while self.running:
            try:
                # wss:// → bağlantı TLS ile şifreli; WebSocket kendi ping'leriyle canlı kalır
                with connect(RELAY_URL, open_timeout=20, ping_interval=25, ping_timeout=20,
                             max_size=MAX_LINE) as ws:
                    with self.ws_lock:
                        self.ws = ws
                    delay = 2
                    self.join_room()
                    for raw in ws:
                        self.on_packet(raw)
            except Exception:
                pass
            finally:
                with self.ws_lock:
                    self.ws = None
            time.sleep(delay)
            delay = min(delay * 2, 60)          # bağlantı yoksa giderek daha seyrek dene

    def start(self):
        server = self.open_ipc()
        threading.Thread(target=self.handle_remote, daemon=True).start()
        self.serve_ipc(server)


if __name__ == "__main__":
    YakishiCore().start()

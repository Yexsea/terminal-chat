import socket
import threading
import os

# Render port atarsa onu kullan, yoksa 10000
PORT = int(os.environ.get("PORT", 10000))
clients = []

def handle_client(conn, addr):
    print(f"[+] Yeni bağlantı: {addr}")
    try:
        # İlk mesajı al
        data = conn.recv(4096)
        if not data:
            conn.close()
            return
            
        # MÜHENDİSLİK HİLESİ: Render'ın "Health Check" botu HTTP GET isteği atar.
        # Eğer bu bir HTTP isteğiyse, onu kandırıp "200 OK" dönüyoruz ki sunucuyu kapatmasın.
        if data.startswith(b"GET") or data.startswith(b"POST"):
            conn.send(b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\nYakishi Server Alive")
            conn.close()
            return
            
        # Eğer HTTP değilse (Yani bizim Python programımız bağlandıysa), listeye ekle
        clients.append(conn)
        
        # İlk gelen veriyi de hemen diğerlerine ilet
        broadcast(data, conn)

        # Dinlemeye devam et
        while True:
            data = conn.recv(4096)
            if not data:
                break
            broadcast(data, conn)
            
    except Exception as e:
        print(f"[-] Hata ({addr}): {e}")
        
    finally:
        if conn in clients:
            clients.remove(conn)
        conn.close()
        print(f"[-] Bağlantı koptu: {addr}")

def broadcast(data, sender_conn):
    # Gelen veriyi (şifreli paketi) gönderen hariç herkese fırlat
    for c in clients:
        if c != sender_conn:
            try:
                c.send(data)
            except:
                if c in clients:
                    clients.remove(c)

def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("0.0.0.0", PORT))
    server.listen()
    print(f"[*] Yakishi Relay Server {PORT} portunda dinliyor...")

    while True:
        conn, addr = server.accept()
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()

if __name__ == "__main__":
    main()
import socket
import threading
import time
import os
import hashlib
import json
import tkinter as tk
from tkinter import scrolledtext, filedialog, messagebox
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- Configuración Global ---
BROADCAST_PORT = 5000       # Puerto para descubrimiento UDP
FILE_TRANSFER_PORT = 5001   # Puerto TCP para transferencia de archivos
BUFFER_SIZE = 4096          # Tamaño del buffer para transferencias
SHARED_FOLDER = "shared_folder" # Carpeta a monitorear
BEACON_INTERVAL = 3         # Segundos entre anuncios de presencia

class Utils:
    """Clase de utilidades estáticas."""
    
    @staticmethod
    def get_local_ip():
        """Obtiene la IP local de la máquina en la red."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # No se necesita conexión real, solo para determinar la interfaz de salida
            s.connect(('8.8.8.8', 1))
            IP = s.getsockname()[0]
        except Exception:
            IP = '127.0.0.1'
        finally:
            s.close()
        return IP

    @staticmethod
    def calculate_file_hash(filepath):
        """Calcula el hash SHA-256 de un archivo para verificar integridad."""
        sha256_hash = hashlib.sha256()
        try:
            with open(filepath, "rb") as f:
                for byte_block in iter(lambda: f.read(4096), b""):
                    sha256_hash.update(byte_block)
            return sha256_hash.hexdigest()
        except FileNotFoundError:
            return None

class NodeDiscoverer:
    """Maneja el descubrimiento de pares usando UDP Broadcasting."""
    
    def __init__(self, callback_update_peers):
        self.peers = {} # Diccionario {ip: last_seen_timestamp}
        self.running = False
        self.local_ip = Utils.get_local_ip()
        self.callback_update_peers = callback_update_peers
        
        # Socket para enviar broadcasts
        self.sock_send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock_send.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        
        # Socket para escuchar broadcasts
        self.sock_recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock_recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # En Linux/Unix a veces SO_REUSEPORT es necesario si múltiples procesos corren en misma máquina
        try:
            self.sock_recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass # No disponible en todos los sistemas
            
        self.sock_recv.bind(('', BROADCAST_PORT))
        self.sock_recv.settimeout(1.0) # Timeout para no bloquear el hilo infinitamente

    def start(self):
        self.running = True
        threading.Thread(target=self._broadcast_loop, daemon=True).start()
        threading.Thread(target=self._listen_loop, daemon=True).start()
        threading.Thread(target=self._cleanup_loop, daemon=True).start()

    def _broadcast_loop(self):
        """Envía un mensaje 'HELLO' periódicamente."""
        message = json.dumps({"type": "HELLO", "ip": self.local_ip}).encode('utf-8')
        while self.running:
            try:
                self.sock_send.sendto(message, ('<broadcast>', BROADCAST_PORT))
            except Exception as e:
                print(f"Error broadcasting: {e}")
            time.sleep(BEACON_INTERVAL)

    def _listen_loop(self):
        """Escucha mensajes 'HELLO' de otros nodos."""
        while self.running:
            try:
                data, addr = self.sock_recv.recvfrom(1024)
                message = json.loads(data.decode('utf-8'))
                
                if message["type"] == "HELLO":
                    peer_ip = message["ip"]
                    # Ignorar nuestra propia IP
                    if peer_ip != self.local_ip:
                        self.peers[peer_ip] = time.time()
                        self.callback_update_peers(self.peers.keys())
            except socket.timeout:
                continue
            except Exception as e:
                print(f"Error listening discovery: {e}")

    def _cleanup_loop(self):
        """Elimina pares que no han enviado señales recientemente."""
        while self.running:
            current_time = time.time()
            # Crear lista de peers a eliminar (inactivos por más de 10s)
            expired_peers = [ip for ip, last_seen in self.peers.items() if current_time - last_seen > 10]
            
            if expired_peers:
                for ip in expired_peers:
                    del self.peers[ip]
                self.callback_update_peers(self.peers.keys())
            
            time.sleep(5)

class FileTransferHandler:
    """Maneja el envío y recepción de archivos vía TCP."""
    
    def __init__(self, log_callback):
        self.log_callback = log_callback
        self.running = False
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(('', FILE_TRANSFER_PORT))
        self.server_socket.listen(5)
        self.server_socket.settimeout(1.0) # Timeout para permitir cierre limpio
        
        # Evitar re-procesar archivos que acabamos de recibir
        self.recently_received = set() 

    def start_server(self):
        self.running = True
        threading.Thread(target=self._accept_connections, daemon=True).start()

    def _accept_connections(self):
        """Acepta conexiones entrantes para recibir archivos."""
        while self.running:
            try:
                client_sock, addr = self.server_socket.accept()
                threading.Thread(target=self._handle_incoming_file, args=(client_sock,), daemon=True).start()
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    self.log_callback(f"Error aceptando conexión: {e}")

    def _handle_incoming_file(self, conn):
        """Procesa la recepción de un archivo."""
        try:
            # Paso 1: Leer metadatos (JSON header len + JSON header)
            # Protocolo simple: primero envio longitud del header (4 bytes int big endian), luego el header JSON
            header_len_bytes = conn.recv(4)
            if not header_len_bytes: return
            
            header_len = int.from_bytes(header_len_bytes, 'big')
            header_bytes = conn.recv(header_len)
            header = json.loads(header_bytes.decode('utf-8'))
            
            filename = header['filename']
            file_hash = header['hash']
            file_size = header['size']
            
            self.log_callback(f"Recibiendo: {filename} ({file_size} bytes)")
            
            filepath = os.path.join(SHARED_FOLDER, filename)
            
            # Verificar si ya tenemos este archivo con el mismo hash para evitar escritura redundante
            current_hash = Utils.calculate_file_hash(filepath)
            if current_hash == file_hash:
                self.log_callback(f"Ignorado: {filename} ya existe y es idéntico.")
                conn.close()
                return

            # Marcar como recibido recientemente para que Watchdog no lo reenvíe
            self.recently_received.add(filename)

            # Paso 2: Recibir contenido del archivo
            received_bytes = 0
            with open(filepath, 'wb') as f:
                while received_bytes < file_size:
                    chunk = conn.recv(min(BUFFER_SIZE, file_size - received_bytes))
                    if not chunk: break
                    f.write(chunk)
                    received_bytes += len(chunk)

            # Validar integridad
            final_hash = Utils.calculate_file_hash(filepath)
            if final_hash == file_hash:
                self.log_callback(f"Éxito: {filename} recibido correctamente.")
            else:
                self.log_callback(f"Error: Hash mismatch en {filename}.")
                
            # Limpiar la marca de "recientemente recibido" después de un tiempo pequeño
            # para permitir futuras modificaciones legítimas locales
            time.sleep(1) 
            if filename in self.recently_received:
                self.recently_received.remove(filename)

        except Exception as e:
            self.log_callback(f"Error en recepción: {e}")
        finally:
            conn.close()

    def send_file(self, ip, filepath):
        """Envía un archivo a una IP específica."""
        if not os.path.exists(filepath): return
        
        filename = os.path.basename(filepath)
        
        # Evitar enviar lo que acabamos de recibir (Bucle infinito)
        if filename in self.recently_received:
            return

        try:
            file_size = os.path.getsize(filepath)
            file_hash = Utils.calculate_file_hash(filepath)
            
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((ip, FILE_TRANSFER_PORT))
            
            # Preparar Header
            header = {
                "filename": filename,
                "size": file_size,
                "hash": file_hash
            }
            header_bytes = json.dumps(header).encode('utf-8')
            header_len = len(header_bytes).to_bytes(4, 'big')
            
            # Enviar Header
            s.sendall(header_len)
            s.sendall(header_bytes)
            
            # Enviar Cuerpo
            with open(filepath, 'rb') as f:
                while True:
                    chunk = f.read(BUFFER_SIZE)
                    if not chunk: break
                    s.sendall(chunk)
            
            self.log_callback(f"Enviado: {filename} a {ip}")
            s.close()
        except ConnectionRefusedError:
            self.log_callback(f"Fallo conexión con {ip}")
        except Exception as e:
            self.log_callback(f"Error enviando a {ip}: {e}")

class WatchdogHandler(FileSystemEventHandler):
    """Manejador de eventos del sistema de archivos."""
    
    def __init__(self, discoverer, transfer_handler):
        self.discoverer = discoverer
        self.transfer_handler = transfer_handler

    def on_created(self, event):
        if not event.is_directory:
            threading.Thread(target=self._process_event, args=(event.src_path,)).start()

    def on_modified(self, event):
        if not event.is_directory:
            threading.Thread(target=self._process_event, args=(event.src_path,)).start()

    def _process_event(self, filepath):
        # Pequeña pausa para asegurar que el archivo se terminó de escribir
        time.sleep(1.0) # Aumentado tiempo para evitar lecturas parciales
        
        filename = os.path.basename(filepath)
        
        # Verificar si es un archivo que acabamos de recibir
        if filename in self.transfer_handler.recently_received:
            # print(f"DEBUG: Ignorando evento local de {filename} (recibido recientemente)")
            return

        # Propagar a todos los peers conocidos
        peers = list(self.discoverer.peers.keys())
        if not peers:
            return
            
        for peer_ip in peers:
            # No enviamos directamente aqui, llamamos a transfer_handler que chequea hashes y recently_received de nuevo
            self.transfer_handler.send_file(peer_ip, filepath)

class P2PApp:
    """Clase principal de la interfaz gráfica y orquestación."""
    
    def __init__(self, root):
        self.root = root
        self.root.title("P2P LAN File Replicator")
        self.root.geometry("600x400")

        # Asegurar directoria
        if not os.path.exists(SHARED_FOLDER):
            os.makedirs(SHARED_FOLDER)
        
        # UI Elements
        self.lbl_local_ip = tk.Label(root, text=f"Mi IP: {Utils.get_local_ip()}", font=("Arial", 12, "bold"))
        self.lbl_local_ip.pack(pady=5)
        
        self.lbl_peers = tk.Label(root, text="Peers Activos: 0")
        self.lbl_peers.pack()
        
        self.list_peers = tk.Listbox(root, height=5)
        self.list_peers.pack(fill=tk.X, padx=10, pady=5)
        
        self.log_area = scrolledtext.ScrolledText(root, state='disabled', height=10)
        self.log_area.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        self.btn_open_folder = tk.Button(root, text="Abrir Carpeta Compartida", command=self.open_folder)
        self.btn_open_folder.pack(pady=10)

        # Lógica de Negocio
        self.transfer_handler = FileTransferHandler(self.log_message)
        self.transfer_handler.start_server()
        
        self.discoverer = NodeDiscoverer(self.update_peer_list)
        self.discoverer.start()
        
        # Watchdog setup
        self.observer = Observer()
        event_handler = WatchdogHandler(self.discoverer, self.transfer_handler)
        self.observer.schedule(event_handler, SHARED_FOLDER, recursive=False)
        self.observer.start()

        # Shutdown graceful
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        
        self.log_message("Sistema iniciado. Esperando peers...")

    def log_message(self, msg):
        """Agrega mensajes al área de texto de manera thread-safe."""
        def _log():
            self.log_area.config(state='normal')
            self.log_area.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {msg}\n")
            self.log_area.see(tk.END)
            self.log_area.config(state='disabled')
        self.root.after(0, _log)

    def update_peer_list(self, peers):
        """Actualiza la lista visual de peers."""
        def _update():
            self.list_peers.delete(0, tk.END)
            for p in peers:
                self.list_peers.insert(tk.END, p)
            self.lbl_peers.config(text=f"Peers Activos: {len(peers)}")
        self.root.after(0, _update)

    def open_folder(self):
        """Abre la carpeta compartida en el explorador de archivos del OS."""
        import platform
        import subprocess
        
        path = os.path.abspath(SHARED_FOLDER)
        if platform.system() == "Windows":
            os.startfile(path)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def on_close(self):
        """Cierre limpio de hilos y observadores."""
        self.observer.stop()
        self.observer.join()
        self.discoverer.running = False
        self.transfer_handler.running = False
        self.root.destroy()
        os._exit(0) # Forzar cierre de hilos daemon si quedan colgados

if __name__ == "__main__":
    root = tk.Tk()
    app = P2PApp(root)
    root.mainloop()

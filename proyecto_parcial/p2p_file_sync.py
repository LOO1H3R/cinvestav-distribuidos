import socket
import threading
import time
import os
import sys
import hashlib
import json
import tkinter as tk
from tkinter import scrolledtext, filedialog, messagebox, simpledialog, ttk
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- Configuración Global ---
BROADCAST_PORT = 5000       # Puerto para descubrimiento UDP
FILE_TRANSFER_PORT = 5001   # Puerto TCP para transferencia de archivos
BUFFER_SIZE = 4096          # Tamaño del buffer para transferencias
SHARED_FOLDER = "shared_folder" # Carpeta a monitorear
PROTECTED_FOLDER = "protected_folder" # Carpeta protegida
BEACON_INTERVAL = 3         # Segundos entre anuncios de presencia
CHECKPOINT_FILE = "checkpoints.json" # Archivo para guardar estado de descargas

class Utils:
    """Clase de utilidades estáticas."""
    
    @staticmethod
    def load_checkpoints():
        if os.path.exists(CHECKPOINT_FILE):
            try:
                with open(CHECKPOINT_FILE, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}

    @staticmethod
    def save_checkpoint(filename, data):
        checkpoints = Utils.load_checkpoints()
        checkpoints[filename] = data
        with open(CHECKPOINT_FILE, 'w') as f:
            json.dump(checkpoints, f)

    @staticmethod
    def remove_checkpoint(filename):
        checkpoints = Utils.load_checkpoints()
        if filename in checkpoints:
            del checkpoints[filename]
            with open(CHECKPOINT_FILE, 'w') as f:
                json.dump(checkpoints, f)
    
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
        
    def set_request_resume_callback(self, callback):
        self.request_resume_callback = callback

    def _broadcast_loop(self):
        """Envía un mensaje 'HELLO' periódicamente."""
        # Pre-calcular mensaje, pero enviar siempre
        while self.running:
            try:
                # Re-check de IP por si cambia la red
                current_ip = Utils.get_local_ip()
                if current_ip != self.local_ip:
                    self.local_ip = current_ip
                
                message = json.dumps({"type": "HELLO", "ip": self.local_ip}).encode('utf-8')
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
                    # Check for pending checkpoint logic
                    self._check_pending_download(peer_ip)

            except socket.timeout:
                continue
            except Exception as e:
                print(f"Error listening discovery: {e}")

    def _check_pending_download(self, peer_ip):
         checkpoints = Utils.load_checkpoints()
         for filename, info in checkpoints.items():
            if info["source_ip"] == peer_ip:
                print(f"DEBUG: Encontrado descarga pendiente de {filename} desde {peer_ip}. Solicitando reanudación.")
                # We need access to send logic. Let's make this simple:
                # Need FileTransferHandler reference or a mechanism to trigger Request
                # For now, let's inject a callback or allow external trigger
                if hasattr(self, 'request_resume_callback'):
                    self.request_resume_callback(peer_ip, filename, info["offset"], info["is_protected"], info.get("password"))


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
    
    def __init__(self, log_callback, password, progress_callback=None, app=None):
        self.log_callback = log_callback
        self.password = password
        self.progress_callback = progress_callback
        self.app = app # Reference to main app for callbacks
        self.running = False
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(('', FILE_TRANSFER_PORT))
        self.server_socket.listen(5)
        self.server_socket.settimeout(1.0) # Timeout para permitir cierre limpio
        
        # Evitar re-procesar archivos que acabamos de recibir
        self.recently_received = set() 
        self.active_downloads = set() # Track active downloads (filename, peer_ip) 

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
            
            # Handle Resume Request
            if header.get("type") == "RESUME_REQUEST":
                req_filename = header["filename"]
                req_offset = header["offset"]
                req_protected = header["is_protected"]
                req_pwd = header.get("password")
                
                # Security check
                if req_protected and req_pwd != self.password:
                    self.log_callback(f"Rechazado Resume: {req_filename} (Password incorrecto)")
                    conn.close()
                    return

                # Check file exists locally
                local_dir = PROTECTED_FOLDER if req_protected else SHARED_FOLDER
                local_path = os.path.join(local_dir, req_filename)
                
                if os.path.exists(local_path):
                     # Trigger send from offset
                     peer_ip = conn.getpeername()[0]
                     conn.close()
                     # Launch send thread
                     threading.Thread(target=self.send_file, 
                                    args=(peer_ip, local_path, req_pwd, req_offset)).start()
                else:
                    self.log_callback(f"No se encontró archivo solicitado para resume: {req_filename}")
                    conn.close()
                return

            filename = header['filename']
            file_hash = header['hash']
            file_size = header['size']
            is_protected = header.get('is_protected', False)
            password = header.get('password', None)
            
            # Avoid duplicate downloads
            peer_ip = conn.getpeername()[0]
            if (filename, peer_ip) in self.active_downloads:
                self.log_callback(f"Ignorado: {filename} ya se esta descargando de {peer_ip}.")
                conn.close()
                return
            
            self.active_downloads.add((filename, peer_ip))

            if is_protected:
                if password != self.password:
                    self.log_callback(f"Rechazado: {filename} (Contraseña incorrecta)")
                    self.active_downloads.remove((filename, peer_ip))
                    conn.close()
                    return
                self.log_callback(f"Recibiendo protegido: {filename} ({file_size} bytes)")
                filepath = os.path.join(PROTECTED_FOLDER, filename)
            else:
                self.log_callback(f"Recibiendo: {filename} ({file_size} bytes)")
                filepath = os.path.join(SHARED_FOLDER, filename)
            
            # Verificar si ya tenemos este archivo con el mismo hash para evitar escritura redundante
            if not os.path.exists(filepath):
                 pass # New file
            else:
                 # If we have a partial file, does it match what we expect?
                 # If hashes match, it's done.
                 # If size matches, it's done.
                 # If size < file_size, maybe partial.
                 current_hash = Utils.calculate_file_hash(filepath)
                 if current_hash == file_hash:
                     self.log_callback(f"Ignorado: {filename} ya existe y es idéntico.")
                     self.active_downloads.remove((filename, peer_ip))
                     conn.close()
                     # If we had a checkpoint, remove it
                     Utils.remove_checkpoint(filename)
                     return

            # Si el archivo existe pero con hash diferente, podría ser una versión vieja o incompleta.
            # Para simplificar checkpoints, asumiremos que si existe y es menor, es el incompleto que estamos bajando.
            existing_size = 0
            if os.path.exists(filepath):
                 existing_size = os.path.getsize(filepath)

            # Check if this is a RESUME (we requested it, so we expect data from offset)
            # The sender sends data assuming offset. But wait, in send_file logic, the header has 'size' = original file size.
            # We are just receiving bytes.
            
            # Marcar como recibido recientemente para que Watchdog no lo reenvíe
            self.recently_received.add((filename, is_protected))

            # Guardar Checkpoint
            if existing_size > 0 and existing_size < file_size:
                 mode = 'ab' # Append
                 received_bytes = existing_size
            else:
                 mode = 'wb' # Overwrite
                 received_bytes = 0
            
            # Add/Update Checkpoint info
            checkpoint_data = {
                 "source_ip": conn.getpeername()[0],
                 "file_size": file_size,
                 "file_hash": file_hash,
                 "is_protected": is_protected,
                 "password": password,
                 "timestamp": time.time(),
                 "offset": received_bytes 
            }
            Utils.save_checkpoint(filename, checkpoint_data)

            with open(filepath, mode) as f:
                while received_bytes < file_size:
                    try:
                        chunk = conn.recv(min(BUFFER_SIZE, file_size - received_bytes))
                        if not chunk: raise ConnectionError("Connection lost")
                        f.write(chunk)
                        received_bytes += len(chunk)
                        
                        # Update Checkpoint periodically?
                        # Updating every chunk is too heavy for disk IO, but let's do simple updates every 1MB or so
                        if received_bytes % (1024*1024) == 0:
                            checkpoint_data["offset"] = received_bytes
                            # Utils.save_checkpoint(filename, checkpoint_data) # Too slow?
                    except Exception as e:
                        self.log_callback(f"Interrupción descarga {filename}: {e}")
                        checkpoint_data["offset"] = received_bytes
                        Utils.save_checkpoint(filename, checkpoint_data)
                        raise # Rethrow to close connection
            
            # Download complete
            Utils.remove_checkpoint(filename)

            # Validar integridad
            final_hash = Utils.calculate_file_hash(filepath)
            if final_hash == file_hash:
                self.log_callback(f"Éxito: {filename} recibido correctamente.")
            else:
                self.log_callback(f"Error: Hash mismatch en {filename}.")
                
            # Limpiar la marca de "recientemente recibido" después de un tiempo prudente
            # Suficiente para que Watchdog dispare sus eventos y sean ignorados
            time.sleep(5) 
            if (filename, is_protected) in self.recently_received:
                self.recently_received.remove((filename, is_protected))

        except Exception as e:
            self.log_callback(f"Error en recepción: {e}")
        finally:
            if 'filename' in locals() and 'peer_ip' in locals():
               if (filename, peer_ip) in self.active_downloads:
                   self.active_downloads.remove((filename, peer_ip))
            conn.close()

    def is_downloading(self, filename, peer_ip):
         return (filename, peer_ip) in self.active_downloads

    def send_file(self, ip, filepath, peer_password=None, offset=0):
        """Envía un archivo a una IP específica."""
        if not os.path.exists(filepath): return
        
        filename = os.path.basename(filepath)
        
        # Determine if protected
        abs_path = os.path.abspath(filepath)
        protected_abs = os.path.abspath(PROTECTED_FOLDER)
        is_protected = abs_path.startswith(protected_abs)

        # Evitar enviar lo que acabamos de recibir (Bucle infinito)
        # Only check if starting from scratch, if resume, we definitely want to send
        if offset == 0:
            if (filename, is_protected) in self.recently_received:
                print(f"DEBUG: No enviando {filename} porque está en recently_received")
                return
        
        pwd_to_send = self.password
        if is_protected and peer_password:
             pwd_to_send = peer_password

        try:
            print(f"DEBUG: Intentando conectar a {ip} para enviar {filename} (Protegido: {is_protected}, Offset: {offset})")
            file_size = os.path.getsize(filepath)
            file_hash = Utils.calculate_file_hash(filepath)
            
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10.0) # Timeout mas largo
            s.connect((ip, FILE_TRANSFER_PORT))
            
            # Preparar Header
            header = {
                "filename": filename,
                "size": file_size, # Total file size, sender knows total
                "hash": file_hash,
                "is_protected": is_protected,
                "password": pwd_to_send if is_protected else None,
                "offset": offset # Sender says where it starts
            }
            header_bytes = json.dumps(header).encode('utf-8')
            header_len = len(header_bytes).to_bytes(4, 'big')
            
            # Enviar Header
            s.sendall(header_len)
            s.sendall(header_bytes)
            
            # Enviar Cuerpo
            sent_bytes = offset
            with open(filepath, 'rb') as f:
                f.seek(offset)
                while True:
                    chunk = f.read(BUFFER_SIZE)
                    if not chunk: break
                    s.sendall(chunk)
                    sent_bytes += len(chunk)
                    if self.progress_callback and file_size > 0:
                        self.progress_callback(filename, sent_bytes, file_size)
            
            self.log_callback(f"Enviado: {filename} a {ip} (Completado)")
            s.close()
        except ConnectionRefusedError:
             self.log_callback(f"Fallo conexión con {ip}")
        except Exception as e:
             self.log_callback(f"Error enviando {filename} a {ip}: {e}")

    def request_resume(self, ip, filename, offset, is_protected, password=None):
        """Solicita reanudación de descarga a un sender"""
        if self.is_downloading(filename, ip):
             # Already downloading
             return

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect((ip, FILE_TRANSFER_PORT))
            
            header = {
                "type": "RESUME_REQUEST",
                "filename": filename,
                "offset": offset,
                "is_protected": is_protected,
                "password": password
            }
            header_bytes = json.dumps(header).encode('utf-8')
            header_len = len(header_bytes).to_bytes(4, 'big')
            
            s.sendall(header_len)
            s.sendall(header_bytes)
            s.close()
            self.log_callback(f"Solicitado RESUME de {filename} a {ip} desde offset {offset}")
        except Exception as e:
            self.log_callback(f"Error solicitando resume: {e}")

class WatchdogHandler(FileSystemEventHandler):
    """Manejador de eventos del sistema de archivos."""
    
    def __init__(self, discoverer, transfer_handler, get_target_peers_callback, get_peer_password_callback):
        self.discoverer = discoverer
        self.transfer_handler = transfer_handler
        self.get_target_peers = get_target_peers_callback
        self.get_peer_password = get_peer_password_callback

    def on_created(self, event):
        if not event.is_directory:
            threading.Thread(target=self._process_event, args=(event.src_path,)).start()

    def on_modified(self, event):
        if not event.is_directory:
            # Use threading to not block the observer loop
            threading.Thread(target=self._process_event, args=(event.src_path,)).start()

    def on_moved(self, event):
        if not event.is_directory:
             # Algunos editores guardan haciendo rename de un temp file al original
             # En este caso, nos interesa el destino (dest_path)
            threading.Thread(target=self._process_event, args=(event.dest_path,)).start()

    def _process_event(self, filepath):
        # Pequeña pausa para asegurar que el archivo se terminó de escribir
        time.sleep(2.0)
        
        if not os.path.exists(filepath):
            return

        filename = os.path.basename(filepath)
        
        # DEBUG: Imprimir para verificar que el evento se está procesando
        print(f"DEBUG: Procesando evento para {filename}") 

        # Determine if protected
        abs_path = os.path.abspath(filepath)
        protected_abs = os.path.abspath(PROTECTED_FOLDER)
        is_protected = abs_path.startswith(protected_abs)

        # Verificar si es un archivo que acabamos de recibir
        if (filename, is_protected) in self.transfer_handler.recently_received:
            print(f"DEBUG: Ignorando {filename} por estar en recently_received")
            return

        # Obtener peers a los que enviar
        peers = self.get_target_peers()
        if not peers:
            print("DEBUG: No hay peers seleccionados o disponibles para enviar.")
            return

        # Calcular hash actual para verificar integridad antes de enviar
        current_hash = Utils.calculate_file_hash(filepath)
        if not current_hash:
            return

        for peer_ip in peers:
            # Obtener contraseña específica del peer si existe
            peer_specific_password = self.get_peer_password(peer_ip)
            
            print(f"DEBUG: Iniciando envío de {filename} a {peer_ip}")
            threading.Thread(target=self.transfer_handler.send_file, 
                           args=(peer_ip, filepath, peer_specific_password)).start()


class P2PApp:
    """Clase principal de la interfaz gráfica y orquestación."""
    
    def __init__(self, root):
        self.root = root
        self.root.withdraw() # Ocultar ventana principal momentáneamente
        
        # Pedir contraseña al inicio
        self.password = simpledialog.askstring("Seguridad", "Ingrese la contraseña para carpetas protegidas:", show='*')
        if not self.password:
            self.password = "secret" # Default fallback o salir? Mejor default por si cancelan
            # O podríamos cerrar la app:
            # sys.exit(0) 
        
        self.root.deiconify() # Mostrar ventana
        self.root.title("P2P LAN File Replicator")
        self.root.geometry("600x400")

        # Asegurar directoria
        if not os.path.exists(SHARED_FOLDER):
            os.makedirs(SHARED_FOLDER)
        if not os.path.exists(PROTECTED_FOLDER):
            os.makedirs(PROTECTED_FOLDER)
        
        # UI Elements
        self.lbl_local_ip = tk.Label(root, text=f"Mi IP: {Utils.get_local_ip()}", font=("Arial", 12, "bold"))
        self.lbl_local_ip.pack(pady=5)
        
        self.lbl_peers = tk.Label(root, text="Peers Activos: 0 (Seleccione uno para enviar solo a ese)")
        self.lbl_peers.pack()
        
        self.peer_passwords = {} # {ip: password}
        self.selected_peer_ip = None
        self.list_peers = tk.Listbox(root, height=5, selectmode=tk.SINGLE)
        self.list_peers.pack(fill=tk.X, padx=10, pady=5)
        self.list_peers.bind('<<ListboxSelect>>', self.on_peer_select)
        
        self.btn_set_peer_pwd = tk.Button(root, text="Set Password for Selected Peer", command=self.set_peer_password, state='disabled')
        self.btn_set_peer_pwd.pack(pady=2)

        # Progress bar
        self.progress_val = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(root, variable=self.progress_val, maximum=100)
        self.progress_bar.pack(fill=tk.X, padx=10, pady=5)
        self.lbl_progress = tk.Label(root, text="")
        self.lbl_progress.pack()

        self.log_area = scrolledtext.ScrolledText(root, state='disabled', height=10)
        self.log_area.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        self.btn_open_folder = tk.Button(root, text="Abrir Carpeta Compartida", command=self.open_folder)
        self.btn_open_folder.pack(pady=5)
        self.btn_open_protected = tk.Button(root, text="Abrir Carpeta Protegida", command=self.open_protected)
        self.btn_open_protected.pack(pady=5)

        # Lógica de Negocio
        self.transfer_handler = FileTransferHandler(self.log_message, self.password, self.update_progress)
        self.transfer_handler.start_server()
        
        self.discoverer = NodeDiscoverer(self.update_peer_list)
        # Link discoverer with transfer request capability
        self.discoverer.set_request_resume_callback(self.transfer_handler.request_resume)
        self.discoverer.start()
        
        # Watchdog setup
        self.observer = Observer()
        event_handler = WatchdogHandler(self.discoverer, self.transfer_handler, self.get_target_peers, self.get_peer_password_safe)
        self.observer.schedule(event_handler, SHARED_FOLDER, recursive=False)
        self.observer.schedule(event_handler, PROTECTED_FOLDER, recursive=False)
        self.observer.start()

        # Shutdown graceful
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        
        self.log_message("Sistema iniciado. Esperando peers...")

    def update_progress(self, filename, sent, total):
        """Callback para actualizar barra de progreso desde el hilo de envío"""
        def _update():
            percent = (sent / total) * 100
            self.progress_val.set(percent)
            self.lbl_progress.config(text=f"Enviando {filename}: {int(percent)}%")
            if sent >= total:
                self.root.after(2000, lambda: self.lbl_progress.config(text="")) # Limpiar mensaje despues de 2s
                self.root.after(2000, lambda: self.progress_val.set(0)) # Reset barra
        self.root.after(0, _update)

    def log_message(self, msg):
        """Agrega mensajes al área de texto de manera thread-safe."""
        def _log():
            self.log_area.config(state='normal')
            self.log_area.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {msg}\n")
            self.log_area.see(tk.END)
            self.log_area.config(state='disabled')
        self.root.after(0, _log)

    def set_peer_password(self):
        selection = self.list_peers.curselection()
        if not selection:
            # Try to use stored selection
             if not self.selected_peer_ip:
                messagebox.showwarning("Error", "Seleccione un peer primero")
                return
        else:
             index = selection[0]
             self.selected_peer_ip = self.list_peers.get(index)
        
        pwd = simpledialog.askstring("Peer Password", f"Password para {self.selected_peer_ip}:", show='*')
        if pwd is not None:
             self.peer_passwords[self.selected_peer_ip] = pwd
             self.log_message(f"Password almacenado para enviar a {self.selected_peer_ip}")

    def get_peer_password_safe(self, ip):
        """Thread-safe getter para passwords de peers"""
        return self.peer_passwords.get(ip)

    def on_peer_select(self, event):
        selection = self.list_peers.curselection()
        if selection:
            index = selection[0]
            self.selected_peer_ip = self.list_peers.get(index)
            # Update label text safely
            self.lbl_peers.config(text=f"Peers Activos: {self.list_peers.size()} (Enviando a: {self.selected_peer_ip})")
            self.btn_set_peer_pwd.config(state='normal')
        else:
            self.selected_peer_ip = None
            self.lbl_peers.config(text=f"Peers Activos: {self.list_peers.size()} (Broadcast a todos)")
            self.btn_set_peer_pwd.config(state='disabled')

    def get_target_peers(self):
        """Retorna la lista de peers destinatarios (seleccionado o todos)."""
        # Snapshot thread-safe de la selección
        target = self.selected_peer_ip
        
        # Validar contra lista actual de peers
        current_peers = list(self.discoverer.peers.keys())
        
        if target:
            if target in current_peers:
                 return [target]
            else:
                # El peer seleccionado se desconectó
                # No enviamos a nadie o fallback a broadcast?
                # Fallback a broadcast es más robusto para reconexiones rápidas, pero confuso.
                # Mejor no enviar si el usuario quería específico y se fue.
                print(f"DEBUG: Peer seleccionado {target} no disponible.")
                return [] 
        
        return current_peers

    def update_peer_list(self, peers):
        """Actualiza la lista visual de peers, preservando selección."""
        def _update():
            # Intentar mantener la selección visual
            current_selection = self.selected_peer_ip
            
            self.list_peers.delete(0, tk.END)
            # Find selection index
            sel_idx = -1
            
            for i, p in enumerate(peers):
                self.list_peers.insert(tk.END, p)
                if p == current_selection:
                    sel_idx = i
            
            if sel_idx >= 0:
                self.list_peers.selection_set(sel_idx) # selection_set no dispara <<ListboxSelect>>
            elif current_selection:
                # Se perdió la selección porque el peer se fue
                self.selected_peer_ip = None
            
            target = self.selected_peer_ip
            suffix = f" (Enviando a: {target})" if target else " (Broadcast a todos)"
            self.lbl_peers.config(text=f"Peers Activos: {len(peers)}{suffix}")
            
            # Update button state
            if target:
                 self.btn_set_peer_pwd.config(state='normal')
            else:
                 self.btn_set_peer_pwd.config(state='disabled')
            
        self.root.after(0, _update)

    def open_folder(self):
        """Abre la carpeta compartida en el explorador de archivos del OS."""
        self._open_folder_path(SHARED_FOLDER)

    def open_protected(self):
        """Abre la carpeta protegida en el explorador de archivos del OS."""
        self._open_folder_path(PROTECTED_FOLDER)

    def _open_folder_path(self, folder):
        path = os.path.abspath(folder)
        if hasattr(os, 'startfile'):
            os.startfile(path)
        else:
            import subprocess
            if 'darwin' in sys.platform:
                subprocess.Popen(['open', path])
            else:
                subprocess.Popen(['xdg-open', path])

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

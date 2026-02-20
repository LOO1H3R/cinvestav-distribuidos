# Guía de Estudio Técnica: Sincronizador de Archivos P2P

Esta guía desglosa el código fuente del proyecto `p2p_file_sync.py` para propósitos de estudio. Se analizan los conceptos clave de programación distribuida, redes y concurrencia implementados.

## 1. Conceptos Fundamentales Implementados

### A. Arquitectura Peer-to-Peer (P2P)
A diferencia de la arquitectura Cliente-Servidor donde hay roles definidos, aquí cada instancia del programa (nodo) ejecuta simultáneamente:
-   **Servidor UDP:** Escucha anuncios de otros nodos (`sock_recv`).
-   **Cliente UDP:** Anuncia su propia presencia (`sock_send`).
-   **Servidor TCP:** Espera conexiones para *recibir* archivos (`server_socket.accept()`).
-   **Cliente TCP:** Inicia conexiones para *enviar* archivos (`socket.connect()`).

### B. Concurrencia y Hilos (Threading)
El programa utiliza múltiples hilos de ejecución (`threads`) para realizar tareas simultáneas sin bloquear la interfaz gráfica (GUI).
-   **Hilo Principal (Main Thread):** Ejecuta el bucle de la GUI (`root.mainloop()`). Si este hilo se bloquea, la ventana se congela.
-   **Hilos Daemon:** Se ejecutan en segundo plano y mueren automáticamente cuando el programa principal se cierra.
    -   `_broadcast_loop`: Envía "HELLO" cada 3s.
    -   `_listen_loop`: Escucha "HELLO" continuamente.
    -   `_accept_connections`: Espera transferencias de archivos entrantes.
    -   `Observer` (Watchdog): Hilo interno de la librería que vigila el disco duro.

---

## 2. Análisis Detallado por Módulos

### Módulo 1: Descubrimiento de Nodos (`NodeDiscoverer`)
**Objetivo:** Saber quién más está conectado a la red local.

#### Protocolo de Capa de Aplicación (Discovery)
-   **Mensaje:** JSON `{"type": "HELLO", "ip": "192.168.1.X"}`
-   **Transporte:** UDP (User Datagram Protocol). Se elige UDP porque es rápido y no requiere establecer conexión ("fire and forget").
-   **Direccionamiento:** Broadcast (`<broadcast>`). Envía el paquete a la dirección `255.255.255.255`, lo que hace que *todos* los dispositivos en la subred reciban el mensaje.

**Código Clave:**
```python
# Configuración para permitir Broadcast
self.sock_send.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

# Configuración para permitir que múltiples programas escuchen el mismo puerto (útil en pruebas locales)
self.sock_recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
```

**Lógica de "Keep-Alive":**
El sistema usa un diccionario `self.peers = {ip: timestamp}`.
-   Cada vez que llega un HELLO, se actualiza el `timestamp`.
-   Un hilo de limpieza (`_cleanup_loop`) revisa cada 5s si `tiempo_actual - timestamp > 10s`. Si es así, asume que el nodo se desconectó y lo borra.

---

### Módulo 2: Transferencia de Archivos (`FileTransferHandler`)
**Objetivo:** Mover bytes de un disco duro a otro de forma fiable.

#### Protocolo de Transferencia (Custom TCP Protocol)
TCP (Transmission Control Protocol) se usa aquí porque garantiza que los datos lleguen en orden y sin errores (vital para archivos).

**Estructura del Paquete:**
1.  **Longitud del Header (4 bytes):** Un entero `big-endian` que dice cuánto mide el siguiente JSON. Esto es necesario porque el JSON tiene longitud variable.
2.  **Header (JSON):** Metadatos:
    ```json
    {
      "filename": "foto.jpg",
      "size": 1024000,
      "hash": "a1b2c3...",
      "is_protected": false,
      "offset": 0
    }
    ```
3.  **Payload (Binario):** El contenido del archivo en bytes crudos.

**Manejo de Buffers (Chunking):**
No se lee todo el archivo en memoria RAM (podría ser de gigabytes). Se lee y envía en trozos (`BUFFER_SIZE = 4096` bytes).
```python
while sent_bytes < file_size:
    chunk = f.read(4096)
    s.sendall(chunk) # Bloqueante hasta que el OS acepta los datos
```

**Integridad de Datos (Hashing):**
Se usa SHA-256.
-   **Emisor:** Calcula hash del archivo original antes de enviar.
-   **Receptor:** Calcula hash del archivo temporal recibido.
-   **Verificación:** Si `hash_emisor == hash_receptor`, la transferencia fue exitosa. Si no, hubo corrupción y el archivo (generalmente) se descarta o marca como erróneo.

**Reanudación (Resume Capability):**
Si una descarga se corta:
1.  Se guarda un "Checkpoint" en `checkpoints.json` con: nombre, hash, y cuántos bytes se recibieron.
2.  Al reconectar, el receptor envía un `RESUME_REQUEST` indicando el `offset` (bytes que ya tiene).
3.  El emisor hace `f.seek(offset)` y envía solo los bytes restantes.
4.  El receptor abre el archivo en modo `'ab'` (append binary) para pegar los nuevos datos al final.

---

### Módulo 3: Monitoreo (`WatchdogHandler`)
**Objetivo:** Reaccionar a cambios en el sistema de archivos (File System).

**Desafío: El "Bucle Infinito" (Echo Loop)**
Si A envía un archivo a B -> B lo escribe en su disco -> El Watchdog de B detecta "Nuevo Archivo" -> B intenta enviarlo de vuelta a A.

**Solución Implementada:**
1.  **Lista `recently_received`:** Cuando el `FileTransferHandler` de B termina de descargar un archivo, agrega su nombre a este conjunto.
2.  **Filtro en Watchdog:** Antes de procesar un evento, pregunta: "¿Acabo de descargar este archivo?". Si sí, ignora el evento y no lo propaga.
3.  **Debounce (Anti-rebote):** Los sistemas operativos a veces lanzan múltiples eventos `modified` para un solo guardado. Se usa un diccionario `last_event_time` para ignorar eventos duplicados que ocurren en menos de 3 segundos.

---

### Módulo 4: Interfaz Gráfica (`P2PApp` con Tkinter)
**Objetivo:** Control visual.

**Thread Safety (Seguridad entre hilos):**
`tkinter` no es *thread-safe*. No se puede modificar la GUI (ej. `label.config()`) directamente desde un hilo secundario (como el de red).
**Solución:** Usar `root.after(0, callback)`. Esto programa la función para que se ejecute en el Hilo Principal tan pronto como sea posible, evitando "Race Conditions" y cierres inesperados.

---

## 3. Preguntas Clave para Estudio

1.  **¿Por qué usamos UDP para Discovery y TCP para Archivos?**
    *   UDP permite *Broadcast* (hablar a todos sin saber quiénes son). TCP requiere una conexión punto-a-punto establecida (IP conocida).
2.  **¿Qué pasa si dos personas editan el mismo archivo al mismo tiempo?**
    *   Este sistema es básico ("Last Write Wins" o carrera de tiempo). No tiene resolución de conflictos como Git. Si A y B guardan al mismo tiempo, el que llegue último sobrescribirá al otro.
3.  **¿Para qué sirve `socket.SO_REUSEADDR`?**
    *   Permite volver a ejecutar el programa inmediatamente después de cerrarlo sin esperar a que el SO libere el puerto (TIME_WAIT state).
4.  **¿Cómo funciona el `f.seek(offset)` en la reanudación?**
    *   Mueve el puntero de lectura del archivo a la posición `offset`. Si el archivo tiene 100 bytes y `offset` es 50, la próxima lectura leerá el byte 51, saltándose los primeros 50.

## 4. Flujo de Ejecución (Paso a Paso)

1.  **Inicio:** `P2PApp` inicia. Se crean los sockets.
2.  **Loop UDP:** `NodeDiscoverer` empieza a gritar "HELLO" y escuchar.
3.  **Usuario copia archivo:** El OS notifica a `watchdog`.
4.  **Evento Watchdog:**
    *   Verifica: ¿Es un archivo temporal? ¿Lo acabo de bajar yo?
    *   Si es genuino nuevo: Llama a `get_target_peers()`.
5.  **Envío TCP:**
    *   Calcula Hash.
    *   Abre conexión TCP a la IP del peer.
    *   Envía Header -> Envía Bytes.
6.  **Recepción TCP:**
    *   Peer recibe Header.
    *   Verifica si tiene el archivo (Hash check).
    *   Si no, descarga bytes -> Verifica Hash Final.

"""DROPIA Clips: se abre en el navegador. Pegas el enlace de un directo y hace todo sola:
chat -> picos -> clips -> verticales con subtítulos (los 3 mejores, si ya sabe dónde está la cámara del canal).
Uso: doble clic en el acceso directo del escritorio, o `python app.py`. Autotest: `python app.py --test`.
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from chat_clips import COOKIES, vid_id, ytdlp

ROOT = Path(__file__).resolve().parent
TRABAJO = ROOT / "trabajo"
CANALES = ROOT / "canales.json"  # canal -> zona de la webcam ("x,y,w,h", o "" si no tiene)
PUERTO = 8765
AUTO = 15  # clips que monta solo al terminar
SIN_VENTANA = 0x08000000 if sys.platform == "win32" else 0  # que no salgan consolas negras
# pythonw no tiene consola: si lanza python/ffmpeg/yt-dlp, Windows abre una ventana negra por cada uno.
# Con python.exe + CREATE_NO_WINDOW todos comparten una consola oculta.
PY = str(Path(sys.executable).with_name("python.exe")) if Path(sys.executable).name.lower() == "pythonw.exe" else sys.executable
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}  # el chat trae emojis
ID_OK = re.compile(r"^[\w-]{1,40}$")

cola = queue.Queue()
estado = {}  # id -> lo que está haciendo ahora
progreso = {}  # id -> {"inicio"} mientras busca y baja, o {"hecho", "total", "desde"} mientras monta
ritmo = [50.0]  # segundos que tardaron los montajes, para estimar lo que falta
subidas = {}  # (id, n) -> "Subiendo a YouTube..." o el error; lo publicado se guarda en publicado.json
subidas_tt = {}  # (id, n) -> lo que hace TikTok o el error; lo publicado se guarda en publicado_tiktok.json
en_vivo = set()  # ids de directos en curso: su propio hilo baja y monta cada clip
pausados = set()  # ids que el usuario pausó: se para la búsqueda y no se monta nada más
procesos = {}  # id -> proceso de chat_clips, para poder pararlo al pausar


class Pausado(Exception):
    pass


def leer(path, defecto):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else defecto


def canal_twitch(url):
    """twitch.tv/<canal> -> su último VOD. Si está en directo es el del directo, que Twitch va grabando mientras emite."""
    m = re.fullmatch(r"(?:https?://)?(?:www\.|m\.)?twitch\.tv/(\w+)/?(?:\?.*)?", url.strip())
    if not m or m[1] in ("videos", "directory"):
        return url
    r = subprocess.run([*ytdlp(), "--flat-playlist", "--playlist-items", "1", "--print", "id",
                        f"https://www.twitch.tv/{m[1]}/videos?filter=archives&sort=time"],
                       capture_output=True, text=True, creationflags=SIN_VENTANA).stdout.split()
    return f"https://www.twitch.tv/videos/{r[0].lstrip('v')}" if r else url


def correr(id_, *args):
    if id_ in pausados:
        raise Pausado()
    with open(TRABAJO / id_ / "log.txt", "a", encoding="utf-8") as log:
        r = subprocess.Popen([PY, *args], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                             env=ENV, creationflags=SIN_VENTANA)
        if args[0] == "chat_clips.py":
            procesos[id_] = r
        r.wait()
    if procesos.get(id_) is r:
        procesos.pop(id_)
    if id_ in pausados:
        raise Pausado()
    if r.returncode:  # la última línea del log suele explicar el fallo
        lineas = [l.strip() for l in (TRABAJO / id_ / "log.txt").read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
        raise RuntimeError(lineas[-1] if lineas else f"falló {args[0]}, mira trabajo/{id_}/log.txt")


def montar(id_, nums, cam, titulos=None, i0=1, total=None):
    total = total or len(nums)
    for i, n in enumerate(nums, i0):
        if id_ in pausados:
            raise Pausado()
        estado[id_] = f"Montando clip {n} ({i} de {total})"
        progreso[id_] = {"hecho": i - 1, "total": total, "desde": time.time()}
        args = ["montar.py", f"trabajo/{id_}", "--n", str(n)]
        if cam:
            args += ["--cam", cam]
        if (titulos or {}).get(str(n)):
            args += ["--titulo", titulos[str(n)]]
        t0 = time.time()
        correr(id_, *args)
        ritmo.append(time.time() - t0)
        progreso[id_] = {"hecho": i, "total": total}
        # El vertical ya está: fuera el horizontal y sus subtítulos para no llenar el disco.
        for f in (f"clip_{n:02}.mp4", f"clip_{n:02}.ass", f"clip_{n:02}.captions.json"):
            try:
                (TRABAJO / id_ / f).unlink(missing_ok=True)
            except OSError:  # shortcut: si el navegador lo tiene abierto se queda; se puede borrar a mano
                pass


def avance(id_):
    """Para la barra: % y segundos que faltan, estimados con lo que tardaron los últimos montajes."""
    p = progreso.get(id_)
    if not p:
        return None
    if "inicio" in p:  # buscando y bajando: no se sabe cuánto falta
        return {"lleva": round(time.time() - p["inicio"])}
    media = sum(ritmo[-5:]) / len(ritmo[-5:])
    va = min(time.time() - p["desde"], media * 0.95) if p.get("desde") else 0
    return {"pct": round(100 * (p["hecho"] + va / media) / p["total"]),
            "resta": round((p["total"] - p["hecho"]) * media - va)}


def mejores(id_, k=AUTO):
    picos = leer(TRABAJO / id_ / "picos.json", [])
    if any(p.get("vivo") for p in picos):  # en directo cada clip ya pasó el filtro de pico: se montan todos
        return [p["n"] for p in picos]
    return [p["n"] for p in sorted(picos, key=lambda p: -p["puntos"])[:k]]


def trabajador():
    while True:
        procesar(cola.get())


def procesar(t):
    id_ = t["id"]
    if t.get("vivo"):
        en_vivo.add(id_)
    try:
        carpeta = TRABAJO / id_
        if id_ in pausados:  # se pausó mientras esperaba en la cola
            raise Pausado()
        if t["tipo"] == "nuevo":
            if "canal" not in t:
                carpeta.mkdir(parents=True, exist_ok=True)
                (carpeta / "error.txt").unlink(missing_ok=True)
                estado[id_] = "Mirando de qué canal es"
                r = subprocess.run([*ytdlp(), "--skip-download", "--print", "uploader",
                                    "--print", "live_status", t["url"]], capture_output=True, text=True, encoding="utf-8",
                                   creationflags=SIN_VENTANA).stdout.splitlines()
                canal = (r[0].strip() if r else "") or "desconocido"
                (carpeta / "meta.json").write_text(json.dumps({"url": t["url"], "canal": canal, "juego": t.get("juego", "")}, ensure_ascii=False),
                                                   encoding="utf-8")
                if "is_live" in r and "twitch.tv" not in t["url"]:  # en Twitch el VOD en curso ya se puede cortar  # un directo dura horas: va en su propio hilo para no frenar la cola
                    threading.Thread(target=procesar, args=({**t, "canal": canal, "vivo": True},), daemon=True).start()
                    return
                t = {**t, "canal": canal}
            canal = t["canal"]
            directo = t.get("vivo")
            espera = "En directo: saco un clip cada vez que el chat explota" if directo else "Bajando el resto de momentos"
            estado[id_] = espera if directo else "Buscando los mejores momentos y bajándolos"
            progreso[id_] = {"inicio": time.time()}
            if not directo:  # que no se monten clips de una pasada anterior; un directo pausado sigue numerando
                (carpeta / "picos.json").unlink(missing_ok=True)
                for viejo in carpeta.glob("clip_*.mp4"):
                    viejo.unlink()
            fallo = []

            def bajar():
                try:
                    correr(id_, "chat_clips.py", t["url"], *(["--juego", t["juego"]] if t.get("juego") else []),
                           *(["--vivo"] if directo else []))
                except Exception as e:
                    fallo.append(e)

            hilo = threading.Thread(target=bajar, daemon=True)
            hilo.start()
            cam = leer(CANALES, {}).get(canal)
            # Canal nuevo: en un video se espera a tener los clips; en directo basta el primero y el directo sigue.
            while cam is None and hilo.is_alive():
                if directo and list(carpeta.glob("clip_*.mp4")):
                    estado[id_] = "Marca dónde está la cámara (el directo sigue grabando)"
                time.sleep(2)
                cam = leer(CANALES, {}).get(canal)
            if cam is None:
                if fallo:
                    raise fallo[0]
                estado[id_] = "Marca dónde está la cámara (solo la primera vez de este canal)"
                progreso.pop(id_, None)
                return
            estado[id_] = espera if directo else estado[id_]
            # chat_clips corta del mejor al peor: cada uno de los AUTO mejores (o todos en directo) se monta al bajarse.
            hechos = []
            while id_ not in pausados:
                vivo = hilo.is_alive()
                nums = mejores(id_) if (carpeta / "picos.json").exists() else []
                listos = [n for n in nums if n not in hechos and (carpeta / f"clip_{n:02}.mp4").exists()]
                if listos:
                    hechos.append(listos[0])
                    montar(id_, listos[:1], cam, i0=len(hechos), total=len(nums))
                    if vivo:
                        estado[id_] = espera
                elif not vivo:
                    break
                else:
                    time.sleep(2)
            if fallo or id_ in pausados:
                raise fallo[0] if fallo else Pausado()
        else:
            montar(id_, t["nums"], leer(CANALES, {}).get(t.get("canal", ""), ""), t.get("titulos"))
        estado[id_] = "Listo"
        progreso.pop(id_, None)
    except Pausado:
        estado[id_] = "Pausado"
        progreso.pop(id_, None)
    except Exception as e:
        estado[id_] = f"Error: {e}"
        progreso.pop(id_, None)
        try:  # se guarda para que el aviso siga ahí aunque se reinicie la app
            (TRABAJO / id_ / "error.txt").write_text(estado[id_], encoding="utf-8")
        except OSError:
            pass
    finally:
        if t.get("vivo"):
            en_vivo.discard(id_)


def hashtags(meta):
    """Sugeridos. Los primeros son los que caben en el título y los 3 primeros salen sobre él: lo buscado va antes
    que #Shorts (YouTube ya reconoce el Short por ser vertical y corto)."""
    canal = re.sub(r"\W", "", meta.get("canal") or "") if meta.get("canal") != "desconocido" else ""
    if "grand theft auto" in (meta.get("juego") or "").lower():
        tags = ["GTARP", "GTA5", canal, "FiveM", "GTAV", "GTA", "Shorts", "gaming", "clips"]
    else:
        tags = [canal, re.sub(r"\W", "", meta.get("juego") or ""), "Shorts", "gaming", "clips", "streamer"]
    return list(dict.fromkeys(t for t in tags if t))  # sin repetidos


def limpiar_tags(texto):
    """Lo que escribe el usuario ('#gta ibai, #GTA') -> ['gta', 'ibai', 'Shorts']: sin #, sin repetidos, en su orden,
    #Shorts al final si no lo puso, máximo 15 (con más de 60 YouTube ignora todos)."""
    tags, vistos = [], set()
    for t in re.findall(r"[^\s#,]+", texto):
        t = re.sub(r"\W", "", t)
        if t and t.lower() not in vistos:
            vistos.add(t.lower())
            tags.append(t)
    return tags[:15] if "shorts" in vistos else tags[:14] + ["Shorts"]


def con_hashtags(titulo, tags):
    """En Shorts el título se ve sobre el video y la descripción casi no: se añaden los hashtags que quepan (máx. 100)."""
    for t in tags:
        if f"#{t}".lower() not in titulo.lower() and len(titulo) + len(t) + 2 <= 100:
            titulo += f" #{t}"
    return titulo


def publicar(id_, n, titulo, texto_tags=""):
    import youtube  # carga la librería de Google solo al publicar
    try:
        tags = limpiar_tags(texto_tags) if texto_tags.strip() else hashtags(leer(TRABAJO / id_ / "meta.json", {}))
        url, privacidad = youtube.subir(TRABAJO / id_ / f"final_{n:02}.mp4", con_hashtags(titulo, tags),
                                        titulo + "\n\n" + " ".join("#" + t for t in tags), tags)
        archivo = TRABAJO / id_ / "publicado.json"
        pub = leer(archivo, {})
        pub[str(n)] = {"url": url, "privacidad": privacidad, "titulo": titulo}
        archivo.write_text(json.dumps(pub, ensure_ascii=False, indent=1), encoding="utf-8")
        subidas.pop((id_, n), None)
    except Exception as e:
        subidas[(id_, n)] = f"No se pudo publicar: {e}"


def duracion(video):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                                capture_output=True, text=True, creationflags=SIN_VENTANA).stdout.strip() or 0)


def publicar_tt(id_, n, titulo, texto_tags, opc):
    import tiktok
    k = (id_, n)
    try:
        tags = limpiar_tags(texto_tags) if texto_tags.strip() else hashtags(leer(TRABAJO / id_ / "meta.json", {}))
        pid = tiktok.subir(TRABAJO / id_ / f"final_{n:02}.mp4", (titulo + " " + " ".join("#" + t for t in tags))[:2200],
                           opc["privacidad"], opc["comentar"], opc["duo"], opc["stitch"], opc["propia"], opc["ajena"])
        for _ in range(120):  # hasta 10 min procesando
            subidas_tt[k] = "TikTok lo está procesando (puede tardar unos minutos)..."
            time.sleep(5)
            e = tiktok.estado(pid)
            if e.get("status") == "FAILED":
                raise RuntimeError(e.get("fail_reason") or "TikTok no lo aceptó")
            if e.get("status") == "PUBLISH_COMPLETE":
                archivo = TRABAJO / id_ / "publicado_tiktok.json"
                pub = leer(archivo, {})
                ids = e.get("publicaly_available_post_id") or []  # así lo escribe TikTok
                pub[str(n)] = {"publish_id": pid, "privacidad": opc["privacidad"], "titulo": titulo,
                               "url": f"https://www.tiktok.com/@{opc['usuario']}/video/{ids[0]}" if ids else None}
                archivo.write_text(json.dumps(pub, ensure_ascii=False, indent=1), encoding="utf-8")
                return subidas_tt.pop(k, None)
        subidas_tt[k] = "TikTok sigue procesándolo. Míralo en tu perfil dentro de un rato."
    except Exception as e:
        subidas_tt[k] = f"No se pudo publicar en TikTok: {e}"[:300]


def resumen():
    canales = leer(CANALES, {})
    out = []
    for c in sorted((d for d in TRABAJO.iterdir() if d.is_dir() and not (d / "archivado").exists()), key=lambda d: -d.stat().st_mtime) if TRABAJO.exists() else []:
        meta = leer(c / "meta.json", {})
        pub = leer(c / "publicado.json", {})
        pub_tt = leer(c / "publicado_tiktok.json", {})
        out.append({
            "id": c.name, **meta,
            "cam": canales.get(meta.get("canal")),
            "progreso": avance(c.name),
            "hashtags": " ".join("#" + t for t in hashtags(meta)),  # sugeridos; el usuario los cambia antes de publicar
            "estado": estado.get(c.name) or ("Pausado" if (c / "pausado").exists() else
                                             (c / "error.txt").read_text(encoding="utf-8") if (c / "error.txt").exists()
                                             else "Listo" if list(c.glob("final_*.mp4")) else ""),
            "picos": [{**p, "clip": (c / f"clip_{p['n']:02}.mp4").exists(),
                       "final": (c / f"final_{p['n']:02}.mp4").exists(),
                       "publicado": pub.get(str(p["n"])), "subida": subidas.get((c.name, p["n"])),
                       "tiktok": pub_tt.get(str(p["n"])), "subida_tt": subidas_tt.get((c.name, p["n"]))}
                      for p in leer(c / "picos.json", [])],
        })
    return out


def borrar(id_):
    """Libera el disco. Si se publicó algo se quedan solo sus .json (el panel de resultados los lee) y se oculta."""
    c = TRABAJO / id_
    if not (c / "publicado.json").exists() and not (c / "publicado_tiktok.json").exists():
        shutil.rmtree(c)
        return
    for f in c.iterdir():
        if f.suffix != ".json" or f.name.startswith("chat"):  # el chat pesa decenas de MB y Resultados no lo usa
            f.unlink()
    (c / "archivado").touch()


def herramientas():
    """Lo que la app necesita instalado, para la pantalla de Ajustes."""
    def version(cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20, creationflags=SIN_VENTANA)
            return (r.stdout.splitlines() or [""])[0].strip() or None
        except (OSError, subprocess.TimeoutExpired):
            return None
    return {"ffmpeg": version(["ffmpeg", "-version"]), "yt-dlp": version(["yt-dlp", "--version"]),
            "node": version(["node", "--version"]), "google": (ROOT / "secrets" / "client_secret.json").exists(),
            "cookies": COOKIES.exists(), "sesion_yt": sesion_yt.get("estado"), "navegador": bool(NAVEGADOR)}


# Sesión de YouTube para yt-dlp sin instalar nada: un perfil de Chrome/Edge solo de la app. El Chrome normal no sirve:
# bloquea su base de cookies mientras está abierto y las cifra de forma que yt-dlp no puede leerlas.
NAVEGADOR = next((p for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                              os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
                              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe") if os.path.exists(p)), None)
PERFIL = ROOT / "secrets" / "navegador"
sesion_yt = {}  # "estado": "abierta" mientras la ventana de login está abierta, luego "ok" o el error
CDP = """const ws = new WebSocket(process.argv[1]);
ws.onopen = () => ws.send(JSON.stringify({id: 1, method: "Storage.getCookies"}));
ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id === 1) { console.log(JSON.stringify(m.result.cookies)); process.exit(0); } };
ws.onerror = () => process.exit(1);"""


def netscape(cookies):
    """Cookies del navegador -> formato cookies.txt de yt-dlp, solo las de YouTube y Google."""
    return "# Netscape HTTP Cookie File\n" + "".join(
        "\t".join([c["domain"], "TRUE" if c["domain"].startswith(".") else "FALSE", c["path"], "TRUE" if c["secure"] else "FALSE",
                   str(int(c["expires"])) if c.get("expires", -1) > 0 else "0", c["name"], c["value"]]) + "\n"
        for c in cookies if c["domain"].lstrip(".").endswith(("youtube.com", "google.com")))


def exportar_cookies():
    """Abre el perfil de la app en segundo plano, le pide las cookies y las guarda si hay sesión iniciada."""
    puerto_f = PERFIL / "DevToolsActivePort"
    puerto_f.unlink(missing_ok=True)
    nav = subprocess.Popen([NAVEGADOR, "--headless=new", f"--user-data-dir={PERFIL}", "--remote-debugging-port=0",
                            "--no-first-run", "about:blank"], creationflags=SIN_VENTANA)
    try:
        for _ in range(60):
            if puerto_f.exists() and puerto_f.read_text().strip():
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("El navegador no arrancó en segundo plano")
        import urllib.request
        ws = json.load(urllib.request.urlopen(f"http://127.0.0.1:{puerto_f.read_text().split()[0]}/json/version",
                                              timeout=10))["webSocketDebuggerUrl"]
        r = subprocess.run(["node", "-e", CDP, ws], capture_output=True, text=True, timeout=30, creationflags=SIN_VENTANA)
        if r.returncode:
            raise RuntimeError("No pude leer las cookies del navegador")
        cookies = json.loads(r.stdout)
    finally:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(nav.pid)], capture_output=True, creationflags=SIN_VENTANA)
    if not any(c["name"] == "LOGIN_INFO" for c in cookies):  # la pone youtube.com al iniciar sesión
        return False
    COOKIES.write_text(netscape(cookies), encoding="utf-8")
    return True


def iniciar_sesion_yt():
    try:  # la ventana es un navegador normal, sin depurador: Google no deja iniciar sesión en uno controlado
        subprocess.run([NAVEGADOR, f"--user-data-dir={PERFIL}", "--no-first-run", "--new-window",
                        "https://accounts.google.com/ServiceLogin?continue=https://www.youtube.com/"])
        sesion_yt["estado"] = "ok" if exportar_cookies() else "No has iniciado sesión en YouTube. Vuelve a probar."
    except Exception as e:
        sesion_yt["estado"] = str(e)


def abrir_ventana(url):
    """Abre la app en su propia ventana (Chrome o Edge en modo app, sin pestañas ni barra); si no hay, en el navegador."""
    for exe in (Path(os.environ.get("ProgramFiles", "")) / "Google/Chrome/Application/chrome.exe",
                Path(os.environ.get("ProgramFiles(x86)", "")) / "Google/Chrome/Application/chrome.exe",
                Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
                Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe"):
        if exe.exists():
            return subprocess.Popen([str(exe), f"--app={url}", "--window-size=1280,860"])
    webbrowser.open(url)


def foto(id_, n):
    """Fotograma del clip para ver la jugada y marcar la cámara (se guarda la primera vez)."""
    jpg = TRABAJO / id_ / f"foto_{n:02}.jpg"
    if not jpg.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "20", "-i", str(TRABAJO / id_ / f"clip_{n:02}.mp4"),
                        "-frames:v", "1", "-vf", "scale=640:-1", str(jpg)], creationflags=SIN_VENTANA)
    return jpg


class Web(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(ROOT), **k)

    def log_message(self, *a):
        pass

    def responder(self, cuerpo, tipo="application/json", codigo=200):
        datos = cuerpo if isinstance(cuerpo, bytes) else json.dumps(cuerpo, ensure_ascii=False).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def trozo(self, f):
        """Sirve el archivo por rangos de como mucho 4 MB y lo cierra antes de enviar: si el navegador deja un video
        a medio cargar, el archivo no se queda abierto y Windows deja eliminarlo. De paso se puede saltar en el video."""
        if not f.is_file():
            return self.responder({"error": "no existe"}, codigo=404)
        tam = f.stat().st_size
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        ini = int(m[1]) if m else 0
        fin = min(int(m[2]) if m and m[2] else tam - 1, ini + 4 * 2**20 - 1 if m else tam - 1, tam - 1)
        if ini >= tam:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{tam}")
            self.end_headers()
            return
        with open(f, "rb") as h:
            h.seek(ini)
            datos = h.read(fin - ini + 1)
        self.send_response(206 if m else 200)
        self.send_header("Content-Type", "video/mp4" if f.suffix == ".mp4" else "image/jpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(datos)))
        if m:
            self.send_header("Content-Range", f"bytes {ini}-{fin}/{tam}")
        self.end_headers()
        self.wfile.write(datos)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/":
            return self.responder((ROOT / "ui.html").read_bytes(), "text/html; charset=utf-8")
        if u.path == "/api/youtube":  # canal conectado (o null)
            import youtube
            try:
                return self.responder({"canal": youtube.canal()})
            except Exception:
                return self.responder({"canal": None})
        if u.path == "/api/tiktok":  # cuenta conectada y sus opciones (TikTok pide pedirlas al pintar la pantalla)
            import tiktok
            if not tiktok.APP.exists():
                return self.responder({"creador": None, "falta_app": True})
            try:
                return self.responder({"creador": tiktok.creador()})
            except Exception as e:
                return self.responder({"creador": None, "error": str(e)[:300]})
        if u.path == "/api/resultados":  # panel de lo publicado: vistas e ingresos
            import youtube
            pub = []
            for c in TRABAJO.iterdir() if TRABAJO.exists() else []:
                for n, v in leer(c / "publicado.json", {}).items() if c.is_dir() else []:
                    if not v.get("url"):  # marcado a mano: no se sabe qué video es en YouTube
                        continue
                    pub.append({"id": v["url"].rsplit("/", 1)[-1], "url": v["url"], "titulo": v["titulo"],
                                "canal": leer(c / "meta.json", {}).get("canal", "")})
            try:
                r = youtube.resultados([p["id"] for p in pub])
            except Exception as e:
                return self.responder({"error": f"No pude leer YouTube: {e}"[:300]})
            for p in pub:
                p.update(r["videos"].get(p["id"], {}))
            return self.responder({"publicados": sorted(pub, key=lambda p: p.get("fecha", ""), reverse=True),
                                   "ingresos": r["ingresos"], "aviso_ingresos": r["aviso_ingresos"]})
        if u.path == "/api/ajustes":
            return self.responder(herramientas())
        if u.path == "/api/estado":
            return self.responder(resumen())
        if u.path == "/api/foto" and ID_OK.match(q.get("id", "")) and q.get("n", "").isdigit():
            jpg = foto(q["id"], int(q["n"]))
            return self.responder(jpg.read_bytes(), "image/jpeg") if jpg.exists() else self.responder({}, codigo=404)
        if u.path.startswith("/trabajo/") and u.path.endswith((".mp4", ".jpg")) and ".." not in u.path:
            return self.trozo(ROOT / unquote(u.path).lstrip("/"))  # solo videos y fotos de trabajo/
        if u.path == "/guia/tutorial.mp4":
            return self.trozo(ROOT / "guia" / "tutorial.mp4")
        self.responder({"error": "no existe"}, codigo=404)

    def do_POST(self):
        d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/conectar":  # abre el login de Google en el navegador
            import youtube
            if not (youtube.SECRETS / "client_secret.json").exists():
                return self.responder({"error": "Falta secrets/client_secret.json (la llave de la app en Google Cloud)."}, codigo=400)
            threading.Thread(target=youtube.login, daemon=True).start()
            return self.responder({"ok": True})
        if self.path == "/api/conectar_tt":
            import tiktok
            if not tiktok.APP.exists():
                return self.responder({"error": "Falta secrets/tiktok_app.json con el client_key y client_secret de tu app de TikTok."}, codigo=400)
            threading.Thread(target=tiktok.login, daemon=True).start()
            return self.responder({"ok": True})
        if self.path == "/api/sesion_yt":  # ventana para iniciar sesión; al cerrarla se guardan las cookies
            if not NAVEGADOR:
                return self.responder({"error": "No encuentro Chrome ni Edge en este PC."}, codigo=400)
            if sesion_yt.get("estado") == "abierta":
                return self.responder({"error": "La ventana para iniciar sesión ya está abierta."}, codigo=400)
            sesion_yt["estado"] = "abierta"
            threading.Thread(target=iniciar_sesion_yt, daemon=True).start()
            return self.responder({"ok": True})
        if self.path == "/api/desconectar":  # olvida el permiso de YouTube de este PC
            import youtube
            youtube.TOKEN.unlink(missing_ok=True)
            return self.responder({"ok": True})
        if self.path == "/api/actualizar":  # yt-dlp viejo = Twitch y YouTube dejan de bajar
            r = subprocess.run([PY, "-m", "pip", "install", "-U", "yt-dlp[default]"], capture_output=True, text=True,
                               creationflags=SIN_VENTANA, env=ENV)
            return self.responder({"ok": not r.returncode, "error": None if not r.returncode else r.stderr.strip()[-300:]})
        if self.path == "/api/nuevo":
            d["url"] = canal_twitch(d.get("url", ""))
            try:
                id_ = vid_id(d["url"])
            except SystemExit:
                return self.responder({"error": "No reconozco el enlace (canal o video de Twitch, o video de YouTube)"}, codigo=400)
            if id_ in en_vivo or procesos.get(id_):  # sigue en marcha: lanzarlo otra vez duplicaría la grabación
                return self.responder({"error": "Ese directo ya se está procesando. Páusalo antes de volver a lanzarlo."}, codigo=400)
            estado[id_] = "En cola"
            pausados.discard(id_)  # también sirve de "Continuar"
            (TRABAJO / id_).mkdir(parents=True, exist_ok=True)
            (TRABAJO / id_ / "pausado").unlink(missing_ok=True)
            (TRABAJO / id_ / "archivado").unlink(missing_ok=True)  # si se borró y se vuelve a pegar, que se vea
            cola.put({"tipo": "nuevo", "id": id_, "url": d["url"], "juego": d.get("juego", "").strip()})
            return self.responder({"id": id_})
        if not ID_OK.match(d.get("id", "")):
            return self.responder({"error": "id no válido"}, codigo=400)
        meta = leer(TRABAJO / d["id"] / "meta.json", {})
        if self.path == "/api/publicar":
            n, titulo = d.get("n"), str(d.get("titulo", "")).strip()
            if not str(n).isdigit() or not titulo:
                return self.responder({"error": "Escribe un título antes de publicar."}, codigo=400)
            if not (TRABAJO / d["id"] / f"final_{int(n):02}.mp4").exists():
                return self.responder({"error": "Ese clip aún no está montado."}, codigo=400)
            subidas[(d["id"], int(n))] = "Subiendo a YouTube..."
            threading.Thread(target=publicar, args=(d["id"], int(n), titulo, str(d.get("hashtags", ""))), daemon=True).start()
            return self.responder({"ok": True})
        if self.path == "/api/publicar_tt":
            import tiktok
            n, titulo = d.get("n"), str(d.get("titulo", "")).strip()
            video = TRABAJO / d["id"] / f"final_{int(n):02}.mp4" if str(n).isdigit() else None
            if not titulo or not video or not video.exists():
                return self.responder({"error": "Escribe un título y monta el clip antes de publicar."}, codigo=400)
            try:
                cr = tiktok.creador()  # lo último de la cuenta, no lo que tenía la pantalla
            except Exception as e:
                return self.responder({"error": f"TikTok no responde: {e}"[:300]}, codigo=400)
            if not cr:
                return self.responder({"error": "Conecta tu cuenta de TikTok primero."}, codigo=400)
            opc = {"privacidad": d.get("privacidad"), "usuario": cr.get("creator_username", ""),
                   "comentar": bool(d.get("comentar")) and not cr.get("comment_disabled"),
                   "duo": bool(d.get("duo")) and not cr.get("duet_disabled"),
                   "stitch": bool(d.get("stitch")) and not cr.get("stitch_disabled"),
                   "propia": bool(d.get("propia")), "ajena": bool(d.get("ajena"))}
            if opc["privacidad"] not in cr.get("privacy_level_options", []):
                return self.responder({"error": "Elige quién puede ver el video."}, codigo=400)
            if opc["ajena"] and opc["privacidad"] == "SELF_ONLY":
                return self.responder({"error": "El contenido de marca no puede ser privado."}, codigo=400)
            if duracion(video) > cr.get("max_video_post_duration_sec", 600):
                return self.responder({"error": f"Tu cuenta de TikTok admite videos de hasta {cr['max_video_post_duration_sec']} s."}, codigo=400)
            subidas_tt[(d["id"], int(n))] = "Subiendo a TikTok..."
            threading.Thread(target=publicar_tt, args=(d["id"], int(n), titulo, str(d.get("hashtags", "")), opc), daemon=True).start()
            return self.responder({"ok": True})
        if self.path == "/api/mostrar_clip":  # abre el Explorador con el vertical ya seleccionado, para arrastrarlo a TikTok Studio
            video = TRABAJO / str(d.get("id", "")) / f"final_{int(d['n']):02}.mp4" if ID_OK.match(str(d.get("id", ""))) and str(d.get("n", "")).isdigit() else None
            if not video or not video.exists():
                return self.responder({"error": "Ese clip aún no está montado."}, codigo=400)
            subprocess.Popen(["explorer", f"/select,{video}"])
            return self.responder({"ok": True})
        if self.path == "/api/marcar":  # subido a mano por YouTube o TikTok Studio: la app no se entera sola
            n = str(d.get("n", ""))
            if not n.isdigit() or not (TRABAJO / d["id"] / f"final_{int(n):02}.mp4").exists():
                return self.responder({"error": "Ese clip aún no está montado."}, codigo=400)
            archivo = TRABAJO / d["id"] / ("publicado.json" if d.get("red") == "yt" else "publicado_tiktok.json")
            pub = leer(archivo, {})
            if d.get("quitar"):
                if pub.get(n, {}).get("manual"):  # lo publicado por la API no se desmarca
                    pub.pop(n)
            else:
                pub.setdefault(n, {"manual": True, "fecha": time.strftime("%Y-%m-%d")})
            if pub:
                archivo.write_text(json.dumps(pub, ensure_ascii=False, indent=1), encoding="utf-8")
            else:
                archivo.unlink(missing_ok=True)  # sin nada publicado, el borrado vuelve a quitar la carpeta entera
            return self.responder({"ok": True})
        if self.path == "/api/borrar":
            ids = [i for i in (d.get("ids") or [d["id"]]) if ID_OK.match(str(i)) and (TRABAJO / str(i)).is_dir()]
            ocupados = [i for i in ids if estado.get(i) and not re.match(r"^(Listo|Error|Marca|Pausado)", estado[i])]
            ocupados += [i for (i, _) in {**subidas, **subidas_tt} if i in ids and i not in ocupados]
            ocupados += [i for i in ids if i in en_vivo and i not in ocupados]  # sigue grabando aunque pida la cámara
            if ocupados:
                return self.responder({"error": "Espera a que termine de montar o publicar antes de eliminarlo."}, codigo=400)
            for i in ids:
                try:
                    borrar(i)
                except OSError:  # Windows no deja borrar un video abierto (en el reproductor o en el Explorador)
                    return self.responder({"error": "Hay un video abierto en otro programa. Ciérralo y vuelve a intentarlo."}, codigo=400)
                estado.pop(i, None)
            return self.responder({"ok": True, "borrados": len(ids)})
        if self.path == "/api/pausar":  # para la búsqueda ya; el clip que se esté montando termina para no dejarlo roto
            id_ = d.get("id", "")
            if not ID_OK.match(id_) or not (TRABAJO / id_).is_dir():
                return self.responder({"error": "Ese video no existe"}, codigo=400)
            pausados.add(id_)
            (TRABAJO / id_ / "pausado").touch()
            if p := procesos.get(id_):  # /T: también el yt-dlp que graba el chat
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, creationflags=SIN_VENTANA)
            montando = str(estado.get(id_, "")).startswith("Montando")
            estado[id_] = "Pausando: termina el clip que está montando" if montando else "Pausado"
            return self.responder({"ok": True})
        if self.path == "/api/carpeta":  # abre la carpeta con los videos en el Explorador
            os.startfile(TRABAJO / d["id"])
            return self.responder({"ok": True})
        if self.path == "/api/camara":  # guarda la cámara del canal y monta los mejores
            cam = d.get("cam", "")
            if cam and not re.match(r"^[\d.]+,[\d.]+,[\d.]+,[\d.]+$", cam):
                return self.responder({"error": "cámara no válida"}, codigo=400)
            canales = leer(CANALES, {})
            canales[meta.get("canal", "desconocido")] = cam
            CANALES.write_text(json.dumps(canales, ensure_ascii=False, indent=1), encoding="utf-8")
            if d["id"] in en_vivo:  # el hilo del directo ve la cámara nueva y monta él todos los clips
                return self.responder({"ok": True})
            d["nums"] = d.get("nums") or mejores(d["id"])
        if self.path in ("/api/camara", "/api/montar"):
            nums = [int(n) for n in d.get("nums", []) if str(n).isdigit()]
            estado[d["id"]] = "En cola"
            cola.put({"tipo": "montar", "id": d["id"], "nums": nums, "canal": meta.get("canal", ""),
                      "titulos": d.get("titulos", {})})
            return self.responder({"ok": True})
        self.responder({"error": "no existe"}, codigo=404)


def autotest():
    txt = netscape([{"domain": ".youtube.com", "path": "/", "secure": True, "expires": 1.9e9, "name": "LOGIN_INFO", "value": "x"},
                    {"domain": "twitch.tv", "path": "/", "secure": False, "expires": -1, "name": "a", "value": "b"}])
    assert txt.splitlines()[1] == ".youtube.com	TRUE	/	TRUE	1900000000	LOGIN_INFO	x" and "twitch" not in txt
    assert hashtags({"canal": "Ibai Llanos", "juego": "Grand Theft Auto"})[:4] == ["GTARP", "GTA5", "IbaiLlanos", "FiveM"]
    assert hashtags({"canal": "desconocido", "juego": ""}) == ["Shorts", "gaming", "clips", "streamer"]
    TRABAJO.mkdir(exist_ok=True)
    t = TRABAJO / "_test"
    t.mkdir(exist_ok=True)
    (t / "picos.json").write_text(json.dumps([{"n": 1, "puntos": 5}, {"n": 2, "puntos": 9}, {"n": 3, "puntos": 7},
                                              {"n": 4, "puntos": 1}]), encoding="utf-8")
    assert mejores("_test", 3) == [2, 3, 1]
    assert any(j["id"] == "_test" and len(j["picos"]) == 4 for j in resumen())
    assert limpiar_tags("#gta ibai, #GTA #Shorts #ñ-o") == ["gta", "ibai", "Shorts", "ño"]
    assert limpiar_tags("#gta") == ["gta", "Shorts"]
    assert len(limpiar_tags(" ".join(f"#t{i}" for i in range(40)))) == 15
    assert con_hashtags("Ibai flipa", ["Shorts", "Ibai"]) == "Ibai flipa #Shorts #Ibai"
    assert len(con_hashtags("x" * 95, ["Shorts", "GTA"])) == 100  # #Shorts no cabe, #GTA sí
    progreso["_t"] = {"hecho": 1, "total": 2}
    assert avance("_t") == {"pct": 50, "resta": 50} and avance("_nada") is None
    progreso["_t"] = {"inicio": time.time() - 30}
    assert avance("_t")["lleva"] == 30
    progreso.pop("_t")
    import tiktok
    assert tiktok.trozos(3 * tiktok.MB) == (3 * tiktok.MB, [(0, 3 * tiktok.MB - 1)])  # pequeño: entero
    t_, p_ = tiktok.trozos(50_000_123 + 64 * tiktok.MB)
    assert t_ == 10 * tiktok.MB and p_[-1][1] == 50_000_123 + 64 * tiktok.MB - 1 and len(p_) == (50_000_123 + 64 * tiktok.MB) // t_
    assert all(b + 1 == c for (_, b), (c, _) in zip(p_, p_[1:]))  # sin huecos
    assert ID_OK.match("2926299057") and not ID_OK.match("../secrets")
    for nombre, pub in (("_b1", False), ("_b2", True)):  # sin publicar se va entero; publicado se queda solo el .json
        b = TRABAJO / nombre
        b.mkdir(exist_ok=True)
        (b / "final_01.mp4").write_bytes(b"x")
        if pub:
            (b / "publicado.json").write_text("{}", encoding="utf-8")
        borrar(nombre)
    assert not (TRABAJO / "_b1").exists()
    assert sorted(f.name for f in (TRABAJO / "_b2").iterdir()) == ["archivado", "publicado.json"]
    assert not any(j["id"] == "_b2" for j in resumen())
    shutil.rmtree(TRABAJO / "_b2")
    for f in t.iterdir():
        f.unlink()
    t.rmdir()
    print("autotest ok")


def main():
    if "--test" in sys.argv:
        return autotest()
    url = f"http://127.0.0.1:{PUERTO}"
    try:
        servidor = ThreadingHTTPServer(("127.0.0.1", PUERTO), Web)
    except OSError:  # ya estaba abierta: solo se abre la pestaña
        return abrir_ventana(url)
    TRABAJO.mkdir(exist_ok=True)
    threading.Thread(target=trabajador, daemon=True).start()
    abrir_ventana(url)
    servidor.serve_forever()


if __name__ == "__main__":
    main()

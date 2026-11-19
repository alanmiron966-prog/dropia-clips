"""Encuentra los mejores momentos de un directo mirando el chat y baja solo esos tramos.

Uso:
  python chat_clips.py URL_DEL_DIRECTO [--top 15] [--dur 45] [--antes 20] [--solo-picos]
  python chat_clips.py --test

Funciona con VODs de Twitch (necesita TwitchDownloaderCLI) y con YouTube. En YouTube, si el video no tiene
chat (no fue directo), usa "lo más visto" del video y, si YouTube no lo da, los tramos con más volumen.
Salida en trabajo/<id>/: chat, picos.json y clip_01.mp4, clip_02.mp4...
En YouTube el directo completo se baja de forma temporal (2-4 GB) y se borra al cortar.
En Twitch se baja solo cada tramo.
"""
import argparse
import array
import json
import re
import shutil
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COOKIES = ROOT / "secrets" / "youtube_cookies.txt"


def ytdlp():
    """yt-dlp con tus cookies de YouTube si las hay: cuando YouTube marca el PC pide "confirma que no eres un bot" en todo."""
    base = ["yt-dlp", "--js-runtimes", "node"]  # node resuelve el "n challenge"; sin él YouTube solo da imágenes
    return [*base, "--cookies", str(COOKIES)] if COOKIES.exists() else base
BIN = 10  # segundos por ventana de conteo
CADA = 120  # en directo: segundos entre repasos del chat
RARO = 0.97  # en directo solo se corta un tramo que esté en el 3% con más chat de lo que va de directo
MAX_VIVO = 30  # tope de clips por directo (unos 1.2 GB ya montados)
# Risas, burlas y "clip" pesan doble; votos sueltos (si, no, 1, 2) casi nada: son encuestas del streamer.
RISA = re.compile(r"(ja){2,}|(je){2,}|(js){2,}|\bx+d+\b|kekw|lul|lmao|omegalul|😂|🤣|\bclip|pendej|la cag|nmms|wtf", re.I)
VOTO = re.compile(r"^\W*(s+i+|n+o+|\d{1,3})\W*$", re.I)


def peso(texto):
    return 2 if RISA.search(texto) else 0.25 if VOTO.match(texto) else 1


def vid_id(url):
    m = re.search(r"twitch\.tv/videos/(\d+)|(?:v=|youtu\.be/|/live/)([\w-]{11})", url)
    if not m:
        raise SystemExit(f"No reconozco el enlace: {url}")
    return m.group(1) or m.group(2)


def bajar_chat(url, carpeta):
    if "twitch.tv" in url:
        exe = shutil.which("TwitchDownloaderCLI") or ROOT / "bin" / "TwitchDownloaderCLI.exe"
        if not Path(exe).exists():
            raise SystemExit("Falta TwitchDownloaderCLI (ponlo en bin/).")
        out = carpeta / "chat.json"
        subprocess.run([str(exe), "chatdownload", "--id", vid_id(url), "-o", str(out)], check=True)
        return out
    r = subprocess.run([*ytdlp(), "--no-progress", "--skip-download", "--write-subs", "--sub-langs",
                        "live_chat", "-o", str(carpeta / "chat"), url], capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        raise SystemExit(explicar(r.stderr))
    out = carpeta / "chat.live_chat.json"
    return out if out.exists() else None  # video normal: no fue directo


def explicar(err):
    """Error de yt-dlp -> frase que entienda cualquiera (la app muestra la última línea del log)."""
    if "not a bot" in err:
        return ("YouTube ha bloqueado este PC por bajar mucho (\"confirma que no eres un bot\"). Ve a Ajustes, pulsa "
                "Iniciar sesión en YouTube y vuelve a intentarlo.")
    if "confirm your age" in err:
        return "Este video tiene restricción de edad en YouTube y no se puede bajar sin cuenta. Prueba con otro."
    if "Private video" in err or "members-only" in err or "Join this channel" in err:
        return "Este video es privado o solo para miembros del canal. Prueba con otro."
    if "not available" in err or "unavailable" in err:
        return "YouTube dice que este video no está disponible (borrado o bloqueado en tu país)."
    lineas = [l for l in err.splitlines() if l.startswith("ERROR")]
    return (lineas[-1] if lineas else err.strip().splitlines()[-1] if err.strip() else "yt-dlp falló sin decir por qué")


def de_heatmap(heat, saltar=30):
    """'Lo más visto' de YouTube (0-1 por tramo) -> puntos por ventana de BIN s. El inicio siempre sale alto."""
    score = {}
    for h in heat:
        if h["start_time"] >= saltar:
            b = int((h["start_time"] + h["end_time"]) / 2 // BIN)
            score[b] = max(score.get(b, 0), round(h["value"] * 100, 1))
    return score


def heatmap(url):
    r = subprocess.run([*ytdlp(), "--skip-download", "--print", "%(heatmap)j", url],
                       capture_output=True, text=True, encoding="utf-8")
    txt = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "NA"
    return de_heatmap(json.loads(txt)) if txt.startswith("[") else {}


def volumen(vod, saltar=30):
    """Volumen medio (RMS) de cada ventana de BIN s, de 0 a 100: los gritos y risas suben."""
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(vod), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                         stdout=subprocess.PIPE)
    rms, b = {}, 0
    while chunk := p.stdout.read(BIN * 8000 * 2):
        a = array.array("h", chunk[:len(chunk) // 2 * 2])
        if b * BIN >= saltar and a:
            rms[b] = (sum(x * x for x in a) / len(a)) ** 0.5
        b += 1
    p.wait()
    top = max(rms.values(), default=0) or 1
    return {k: round(v / top * 100, 1) for k, v in rms.items()}


def segmento(lista, seg):
    """(índice, segundo en que empieza) del segmento de la lista HLS `lista` que contiene el segundo `seg`."""
    durs, t = [float(x) for x in re.findall(r"#EXTINF:([\d.]+)", lista)], 0.0
    for i, d in enumerate(durs):
        if t + d > seg:
            return i, t
        t += d
    raise SystemExit(f"Ese momento ({seg:.0f} s) aún no está en el video de Twitch.")


def tramo_twitch(url, fmt, ini, fin, salida):
    """Baja un tramo de un VOD de Twitch. Si el directo sigue en marcha, el VOD es una lista HLS abierta: ffmpeg
    la trata como directo y espera al final, así que se le dice en qué segmento empezar."""
    m3u = subprocess.run([*ytdlp(), "-g", "-f", "b[height<=1080]/b", url], capture_output=True, text=True,
                         check=True).stdout.split()[-1]
    lista = urllib.request.urlopen(m3u, timeout=30).read().decode()
    if "#EXT-X-ENDLIST" in lista:  # VOD terminado
        subprocess.run([*ytdlp(), "--no-progress", "-f", fmt, "--download-sections", f"*{ini}-{fin}",
                        "--force-overwrites", "-o", str(salida), url], check=True)
        return
    i, t = segmento(lista, ini)
    subprocess.run(["ffmpeg", "-v", "error", "-live_start_index", str(i), "-i", m3u, "-ss", str(ini - t),
                    "-t", str(fin - ini), "-c", "copy", "-y", str(salida)], check=True)


def bajar_vod(url, carpeta, fmt):
    vod = carpeta / "vod.mp4"
    if not vod.exists():
        subprocess.run([*ytdlp(), "--no-progress", "-f", fmt,
                        "--merge-output-format", "mp4", "-o", str(vod), url], check=True)
    return vod


def leer_chat(path, juego=None):
    """Devuelve [(segundo, texto)] para el JSON de TwitchDownloader o el live_chat de YouTube.
    Con `juego` (solo Twitch) se quedan los mensajes de los tramos en que se jugaba a ese juego."""
    txt = Path(path).read_text(encoding="utf-8")
    if not str(path).endswith("live_chat.json"):  # Twitch: un solo JSON
        d = json.loads(txt)
        msgs = [(c["content_offset_seconds"], c["message"]["body"]) for c in d["comments"]]
        if juego:
            tramos = [(c["startMilliseconds"] / 1000, (c["startMilliseconds"] + c["lengthMilliseconds"]) / 1000)
                      for c in d["video"].get("chapters", []) if juego.lower() in c["gameDisplayName"].lower()]
            print("Tramos de", juego, tramos or "ninguno")
            msgs = [(t, m) for t, m in msgs if any(i <= t < f for i, f in tramos)]
        return msgs
    msgs = []
    for linea in txt.splitlines():  # YouTube: un JSON por línea
        r = json.loads(linea).get("replayChatItemAction", {})
        for a in r.get("actions", []):
            item = a.get("addChatItemAction", {}).get("item", {}).get("liveChatTextMessageRenderer")
            if item:
                msgs.append((int(r["videoOffsetTimeMsec"]) / 1000, texto(item)))
    return msgs


def texto(item):
    return "".join(run.get("text") or run.get("emoji", {}).get("shortcuts", [""])[0] for run in item["message"].get("runs", []))


def leer_vivo(datos):
    """Chat que yt-dlp va grabando de un directo -> [(hora unix, texto)] y cuántos bytes se usaron.
    En vivo videoOffsetTimeMsec cuenta desde que arrancó yt-dlp, no desde el inicio del video: vale la hora del mensaje."""
    datos = datos[:datos.rfind(b"\n") + 1]  # la última línea puede estar a medio escribir
    msgs = []
    for linea in datos.decode("utf-8", "replace").splitlines():
        for a in json.loads(linea).get("replayChatItemAction", {}).get("actions", []):
            item = a.get("addChatItemAction", {}).get("item", {}).get("liveChatTextMessageRenderer")
            if item:
                msgs.append((int(item["timestampUsec"]) / 1e6, texto(item)))
    return msgs, len(datos)


def trozos(ultimo, dur, ahora, desde, hasta):
    """Números (sq) de los trozos DASH de `dur` s que cubren [desde, hasta] (horas unix); `ultimo` es el trozo de `ahora`."""
    return range(max(0, ultimo - round((ahora - desde) / dur)), min(ultimo, ultimo - round((ahora - hasta) / dur)))


def bajar_tramo_vivo(url, desde, hasta, salida):
    """Baja un tramo de un directo de YouTube en curso pidiendo sus trozos de 2 s por número; devuelve en qué segundo
    del directo empieza. yt-dlp no sabe cortar un directo en curso (--download-sections), así que se piden a mano."""
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "live_from_start": True, "js_runtimes": {"node": {}},
                           **({"cookiefile": str(COOKIES)} if COOKIES.exists() else {})}) as y:
        fs = [f for f in y.extract_info(url, download=False)["formats"] if f.get("protocol") == "http_dash_segments_generator"]
    video = max((f for f in fs if f.get("ext") == "mp4" and f.get("vcodec") != "none" and (f.get("height") or 0) <= 1080),
                key=lambda f: f["height"], default=None)
    audio = next((f for f in fs if f.get("format_id") == "140"), None) or next((f for f in fs if f.get("vcodec") == "none"), None)
    if not video or not audio:
        raise RuntimeError("YouTube ya no da este directo por trozos (¿acaba de terminar?)")
    partes, sqs = [], None
    for f in (video, audio):
        primero = next(f["fragments"]({}))  # trae el número del último trozo publicado
        # Los mismos números para audio y video: cada formato va por su último trozo y, si se calcula aparte,
        # la voz queda ~2 s desplazada de la imagen (y los subtítulos con ella).
        sqs = sqs or trozos(primero["fragment_count"], f["target_duration"], time.time(), desde, hasta)
        inicio = sqs.start * f["target_duration"]

        def uno(sq, base=primero["url"], cab=f.get("http_headers", {})):
            u = re.sub(r"([?&])sq=\d+", rf"\g<1>sq={sq}", base)
            return urllib.request.urlopen(urllib.request.Request(u, headers=cab), timeout=30).read()

        with ThreadPoolExecutor(8) as ex:
            datos = b"".join(ex.map(uno, sqs))
        partes.append(salida.with_name(salida.stem + ("_v.mp4" if f is video else "_a.m4a")))
        partes[-1].write_bytes(datos)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(partes[0]), "-i", str(partes[1]), "-c", "copy", str(salida)], check=True)
    for p in partes:
        p.unlink()
    return inicio


def en_directo(a, carpeta):
    """Directo de YouTube en curso: graba el chat y cada CADA s, si en lo último hubo un pico de los más altos
    de lo que va de directo, baja ese trozo y lo deja como clip_NN.mp4. La app lo monta sin esperar a que acabe."""
    for viejo in carpeta.glob("chat.live_chat.json*"):  # al continuar tras una pausa, el chat se graba de nuevo
        viejo.unlink()
    previos = carpeta / "picos.json"
    lista = [p for p in json.loads(previos.read_text(encoding="utf-8")) if p.get("vivo")] if previos.exists() else []
    grabar =subprocess.Popen([*ytdlp(), "--no-progress", "--skip-download", "--write-subs",
                               "--sub-langs", "live_chat", "-o", str(carpeta / "chat"), a.url],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    score, textos = {}, {}
    pos = revisado = ultimo_b = 0
    sep = a.dur + a.antes
    print("Directo en curso: saco un clip cada vez que el chat explota")
    while True:
        for _ in range(CADA):
            if grabar.poll() is not None:  # acabó el directo
                break
            time.sleep(1)
        # yt-dlp escribe en .part y lo renombra al acabar; se abre y se cierra cada vez para no bloquear ese renombrado
        chat = next((p for p in (carpeta / "chat.live_chat.json.part", carpeta / "chat.live_chat.json") if p.exists()), None)
        if chat:
            with open(chat, "rb") as f:
                f.seek(pos)
                msgs, usado = leer_vivo(f.read())
            pos += usado
            for t, txt in msgs:
                b = int(t // BIN)
                score[b] = score.get(b, 0) + peso(txt)
                textos.setdefault(b, []).append(txt)
        cerrado = int((time.time() - 30) // BIN)  # a estas ventanas ya no les llega chat
        nuevos = [b for b in score if revisado < b < cerrado]
        if nuevos and len(score) >= 30:  # con menos de 5 min de chat no hay con qué comparar
            b = max(nuevos, key=score.get)
            umbral = sorted(score.values())[int(len(score) * RARO)]
            if score[b] >= umbral and (b - ultimo_b) * BIN >= sep:
                n = len(lista) + 1
                tmp = carpeta / f"tmp_{n:02}.mp4"
                try:
                    ini = bajar_tramo_vivo(a.url, b * BIN - a.antes, b * BIN - a.antes + a.dur, tmp)
                except Exception as e:  # un tramo que falla no para el directo
                    print("No pude bajar el tramo:", e)
                else:
                    ultimo_b = b
                    lista.append({"n": n, "pico": ini + a.antes, "inicio": ini, "fin": ini + a.dur,
                                  "puntos": round(score[b], 1), "chat": textos[b][:15], "vivo": True})
                    print(f"#{n}  puntos {lista[-1]['puntos']} (umbral {umbral:.1f}) | {' / '.join(textos[b][:4])}")
                    (carpeta / "picos.json").write_text(json.dumps(lista, ensure_ascii=False, indent=1), encoding="utf-8")
                    listo(tmp, carpeta, n)
        if nuevos and len(score) >= 30:  # durante el calentamiento no se descartan ventanas: sus picos cuentan luego
            revisado = cerrado - 1
        if grabar.poll() is not None or len(lista) >= MAX_VIVO:
            break
    grabar.terminate()
    if not lista:
        raise SystemExit("El directo acabó y el chat nunca tuvo un pico claro.")
    print("Listo:", carpeta)


def picos(msgs, top, separacion, saltar=0):
    """Ventanas de BIN s con más chat (las risas cuentan doble), separadas al menos `separacion` s."""
    score = {}
    for t, texto in msgs:
        if t >= saltar:  # el inicio del directo son saludos, no momentos
            b = int(t // BIN)
            score[b] = score.get(b, 0) + peso(texto)
    return elegir(score, top, separacion)


def elegir(score, top, separacion):
    elegidos = []
    for b, s in sorted(score.items(), key=lambda kv: -kv[1]):
        if all(abs(b - e) * BIN >= separacion for e, _ in elegidos):
            elegidos.append((b, s))
        if len(elegidos) == top:
            break
    return sorted(elegidos)


def listo(tmp, carpeta, n):
    """El clip aparece con su nombre solo cuando está entero; los subtítulos de un clip anterior ya no valen."""
    (carpeta / f"clip_{n:02}.captions.json").unlink(missing_ok=True)
    tmp.replace(carpeta / f"clip_{n:02}.mp4")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--dur", type=int, default=45, help="duración del clip en segundos")
    ap.add_argument("--antes", type=int, default=20, help="segundos antes del pico (el chat reacciona tarde)")
    ap.add_argument("--saltar", type=int, default=300, help="ignorar los primeros N s (saludos)")
    ap.add_argument("--juego", help="solo Twitch: p. ej. \"Grand Theft Auto\"")
    ap.add_argument("--chat", help="usar un chat ya descargado")
    ap.add_argument("--solo-picos", action="store_true", help="no bajar video")
    ap.add_argument("--guardar-vod", action="store_true", help="no borrar el directo completo")
    ap.add_argument("--vivo", action="store_true", help="directo de YouTube en curso: saca clips mientras dura")
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args()
    if a.test:
        return autotest()
    if not a.url:
        ap.error("falta el enlace del directo")

    carpeta = ROOT / "trabajo" / vid_id(a.url)
    carpeta.mkdir(parents=True, exist_ok=True)
    if a.vivo:
        return en_directo(a, carpeta)
    fmt ="bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b"
    sep = a.dur + a.antes
    chat = a.chat or bajar_chat(a.url, carpeta)
    msgs, aviso = [], None
    if chat:
        msgs = leer_chat(chat, a.juego)
        print(f"{len(msgs)} mensajes de chat")
        elegidos = picos(msgs, a.top, sep, a.saltar)
    elif score := heatmap(a.url):
        aviso = "Sin chat: de lo más visto del video"
        elegidos = elegir(score, a.top, sep)
    else:
        aviso = "Sin chat: de los momentos con más volumen"
        elegidos = elegir(volumen(bajar_vod(a.url, carpeta, fmt)), a.top, sep)
    if aviso:
        print(aviso)

    lista = []
    for i, (b, s) in enumerate(elegidos, 1):
        ini = max(0, b * BIN - a.antes)
        muestra = [aviso] if aviso else [m for t, m in msgs if b * BIN <= t < (b + 1) * BIN][:15]
        lista.append({"n": i, "pico": b * BIN, "inicio": ini, "fin": ini + a.dur, "puntos": s, "chat": muestra})
        print(f"#{i}  {ini // 3600}:{ini % 3600 // 60:02}:{ini % 60:02}  puntos {s}  | {' / '.join(muestra[:4])}")
    (carpeta / "picos.json").write_text(json.dumps(lista, ensure_ascii=False, indent=1), encoding="utf-8")

    if not lista:
        raise SystemExit("No encontré momentos en este video.")
    if a.solo_picos:
        return
    # De mejor a peor: la app empieza a montar el mejor en cuanto existe su clip_NN.mp4.
    orden = sorted(lista, key=lambda p: -p["puntos"])
    if "twitch.tv" in a.url:  # Twitch va en trozos HLS: se baja solo cada tramo (unos 8 s por clip)
        for p in orden:
            tmp = carpeta / f"tmp_{p['n']:02}.mp4"
            tramo_twitch(a.url, fmt, p["inicio"], p["fin"], tmp)
            listo(tmp, carpeta, p["n"])
        print("Listo:", carpeta)
        return
    # YouTube frena las peticiones de ffmpeg a medio video, así que se baja el VOD entero (con rangos
    # rápidos de yt-dlp) y se corta en local. Se borra al terminar salvo --guardar-vod.
    vod = bajar_vod(a.url, carpeta, fmt)
    for p in orden:
        tmp = carpeta / f"tmp_{p['n']:02}.mp4"
        # Copia sin recodificar (1 s en vez de ~30): empieza en el fotograma clave anterior, hasta 2 s antes.
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(p["inicio"]), "-i", str(vod), "-t", str(a.dur),
                        "-c", "copy", "-avoid_negative_ts", "make_zero", str(tmp)], check=True)
        listo(tmp, carpeta, p["n"])
    if not a.guardar_vod:
        vod.unlink()
    print("Listo:", carpeta)


def autotest():
    assert vid_id("https://www.twitch.tv/videos/2280000001") == "2280000001"
    assert segmento("#EXTINF:10.000,|0.mp4|#EXTINF:10.000,|1.mp4|#EXTINF:4.5,|2.mp4", 23) == (2, 20.0)
    assert vid_id("https://www.youtube.com/watch?v=4AiW_Ttcl1A") == "4AiW_Ttcl1A"
    assert peso("siii") == peso("100") == 0.25 and peso("yair la cagaste") == 2 and peso("que pasa") == 1
    assert RISA.search("JAJAJA") and RISA.search("jsjs") and RISA.search("KEKW") and not RISA.search("hola ja")
    msgs = [(5, "hola")] + [(100 + i % 10, "jajaja") for i in range(10)] + [(115, "ok")] * 3 \
        + [(300 + i % 10, "wow") for i in range(12)]
    assert picos(msgs, 2, 65, saltar=200) == [(30, 12)]
    p = picos(msgs, 2, 65)
    assert p == [(10, 20), (30, 12)], p  # risas x2 ganan, y 115 s queda absorbido por la separación
    heat = [{"start_time": 0, "end_time": 10, "value": 1.0}, {"start_time": 40, "end_time": 50, "value": 0.8},
            {"start_time": 50, "end_time": 60, "value": 0.3}]
    assert de_heatmap(heat) == {4: 80.0, 5: 30.0}  # el inicio (siempre 1.0) no cuenta
    assert elegir(de_heatmap(heat), 2, 65) == [(4, 80.0)]
    item = {"timestampUsec": "1795000000000000", "message": {"runs": [{"text": "jaja "}, {"emoji": {"shortcuts": [":fire:"]}}]}}
    linea = json.dumps({"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": {"liveChatTextMessageRenderer": item}}}]},
                        "videoOffsetTimeMsec": "5", "isLive": True}).encode()
    assert leer_vivo(linea + b"\n" + linea[:20]) == ([(1795000000.0, "jaja :fire:")], len(linea) + 1)  # la línea a medias espera
    # último trozo 1000 ahora (t=10000), trozos de 2 s: hace 100 s -> 950; hasta hace 54 s -> sin incluir 973
    assert trozos(1000, 2, 10000, 9900, 9946) == range(950, 973)
    assert trozos(1000, 2, 10000, 9990, 10030) == range(995, 1000)  # nunca pide trozos que aún no existen
    print("autotest ok")


if __name__ == "__main__":
    main()

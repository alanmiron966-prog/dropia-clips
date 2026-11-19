"""Convierte los clips de chat_clips.py en videos 9:16 con subtítulos.

Por defecto monta con ffmpeg (rápido y gratis). Con --remotion usa el editor Remotion (reels-editor):
más lento y, si se vende la app, con licencia de pago.

Uso:
  python montar.py trabajo/<id> [--n 1,3] [--cam x,y,w,h] [--juego x,y,w,h] [--titulo "..."] [--remotion]
  python montar.py --test

--cam: zona de la webcam en fracciones del video (0-1), p. ej. 0,0,0.25,0.3 = arriba a la izquierda.
       Con cam sale cámara arriba y juego abajo; sin cam, el video entero sobre fondo desenfocado.
El título de cada clip sale de picos.json ("titulo") o de --titulo. Salida: final_01.mp4...
"""
import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EDITOR = Path(r"C:\Users\HP_Store\Desktop\Claude\Dropia Instagram\contenido\reels-editor")
W, H, CAM_H = 1080, 1920, 700  # mismas medidas que ClipStream.tsx
TEXTO, ACENTO, FONDO = "&H00DCE9F3", "&H003D8AFF", "&H000A0D12"  # colores de estilo.tsx en ASS (&HAABBGGRR)
_modelo = None


def recorte(txt):
    if not txt:
        return None
    x, y, w, h = (float(v) for v in txt.split(","))
    assert 0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1, "fracciones entre 0 y 1"
    return {"x": x, "y": y, "w": w, "h": h}


def transcribir(clip, salida):
    """Subtítulos palabra a palabra (formato Caption de Remotion). VAD y sin arrastrar texto previo:
    con música y gritos de directo Whisper suele repetir la misma frase en bucle."""
    global _modelo
    if _modelo is None:  # se carga una sola vez aunque se monten varios clips
        from faster_whisper import WhisperModel
        _modelo = WhisperModel("small", device="cpu", compute_type="int8")
    segs, _ = _modelo.transcribe(str(clip), language="es", word_timestamps=True, vad_filter=True,
                                 condition_on_previous_text=False)
    caps = []
    for seg in segs:
        for w in seg.words:
            ini, fin = int(w.start * 1000), int(w.end * 1000)
            texto = " " + w.word.strip()
            if caps and texto == caps[-1]["text"] == (caps[-2]["text"] if len(caps) > 1 else None):
                continue  # tercera repetición seguida de la misma palabra: alucinación
            caps.append({"text": texto, "startMs": ini, "endMs": fin, "timestampMs": (ini + fin) // 2,
                         "confidence": round(w.probability, 3)})
    salida.write_text(json.dumps(caps, ensure_ascii=False), encoding="utf-8")


def paginas(caps, juntar=900):
    """Frases cortas como createTikTokStyleCaptions de Remotion: palabras a menos de `juntar` ms del inicio de la frase."""
    pags = []
    for c in caps:
        if pags and c["startMs"] - pags[-1][0]["startMs"] < juntar:
            pags[-1].append(c)
        else:
            pags.append([c])
    return pags


def t_ass(ms):
    cs = int(ms) // 10
    return f"{cs // 360000}:{cs // 6000 % 60:02}:{cs // 100 % 60:02}.{cs % 100:02}"


def limpio(txt):
    return txt.replace("\\", "").replace("{", "(").replace("}", ")").strip().upper()


def ass(caps, titulo, cam):
    """Subtítulos (palabra actual en naranja) y título de los 4 primeros segundos, en formato ASS para ffmpeg."""
    lineas = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        f"Style: Sub,Anton,155,{TEXTO},{TEXTO},&H00000000,&H99000000,0,0,0,0,100,100,0,0,1,7,3,8,60,60,"
        f"{1380 if cam else 1420},1",
        f"Style: Titulo,Anton,140,{TEXTO},{TEXTO},{FONDO},{FONDO},0,0,0,0,100,100,0,0,3,18,0,8,90,90,"
        f"{CAM_H + 60 if cam else 200},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if titulo:
        lineas.append(f"Dialogue: 1,{t_ass(0)},{t_ass(4000)},Titulo,,0,0,0,,{{\\fad(200,250)}}{limpio(titulo)}")
    pags = paginas(caps)
    for i, pag in enumerate(pags):
        fin_pag = pags[i + 1][0]["startMs"] if i + 1 < len(pags) else pag[-1]["endMs"]
        for j, w in enumerate(pag):
            ini = pag[0]["startMs"] if j == 0 else w["startMs"]
            fin = pag[j + 1]["startMs"] if j + 1 < len(pag) else fin_pag
            if fin <= ini:
                continue
            txt = " ".join(f"{{\\c{ACENTO}}}{limpio(x['text'])}{{\\c{TEXTO}}}" if k == j else limpio(x["text"])
                           for k, x in enumerate(pag))
            lineas.append(f"Dialogue: 0,{t_ass(ini)},{t_ass(fin)},Sub,,0,0,0,,{txt}")
    return "\n".join(lineas) + "\n"


def zona(r, ancho, alto, iw, ih):
    """Filtro ffmpeg equivalente a <Zona> de ClipStream: la zona r llena la caja ancho x alto, centrada en ella."""
    escala = max(ancho / (r["w"] * iw), alto / (r["h"] * ih))
    cw, ch = min(iw, ancho / escala), min(ih, alto / escala)
    x = min(max(0, (r["x"] + r["w"] / 2) * iw - cw / 2), iw - cw)
    y = min(max(0, (r["y"] + r["h"] / 2) * ih - ch / 2), ih - ch)
    return f"crop={int(cw)}:{int(ch)}:{int(x)}:{int(y)},scale={ancho}:{alto},setsar=1"


def montar_ffmpeg(clip, salida, dur, subs_ass, cam, juego):
    iw, ih = (int(v) for v in subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0",
         str(clip)], capture_output=True, text=True, check=True).stdout.strip().split(","))
    rel = subs_ass.relative_to(ROOT).as_posix()  # ruta relativa: el filtro ass no acepta "C:" sin escapar
    if cam:
        centro = juego or {"x": 0.2, "y": 0, "w": 0.6, "h": 1}
        grafo = (f"[0:v]fps=30,split=2[a][b];[a]{zona(cam, W, CAM_H, iw, ih)}[c];"
                 f"[b]{zona(centro, W, H - CAM_H, iw, ih)}[j];"
                 f"[c][j]vstack,drawbox=y={CAM_H - 5}:w={W}:h=10:color=0xff8a3d:t=fill")
    else:  # el fondo se desenfoca a 1/4 de tamaño: mismo aspecto y mucho más rápido
        alto = round(W * (1080 / 1920) * 1.35)
        grafo = (f"[0:v]fps=30,split=2[a][b];"
                 f"[a]{zona({'x': 0.3, 'y': 0, 'w': 0.4, 'h': 1}, W // 4, H // 4, iw, ih)},"
                 f"gblur=sigma=10,colorchannelmixer=rr=.5:gg=.5:bb=.5,scale={W}:{H}[f];"
                 f"[b]{zona({'x': 0, 'y': 0, 'w': 1, 'h': 1}, W, alto, iw, ih)}[v];[f][v]overlay=0:560")
    grafo += f",ass=filename={rel}:fontsdir=fuentes[out]"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(clip), "-t", f"{dur:.2f}", "-filter_complex", grafo,
                    "-map", "[out]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(salida)],
                   cwd=ROOT, check=True)


def montar_remotion(carpeta, n, clip, dur, subs, titulo, a):
    rel = f"clips/{carpeta.name}_{n:02}.mp4"
    (EDITOR / "public" / "clips").mkdir(exist_ok=True)
    # A 30 fps y con fotograma clave cada segundo el render va más ligero (los directos vienen a 60).
    # ultrafast + crf 16: archivo temporal más grande pero el doble de rápido y sin perder calidad.
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(clip), "-r", "30", "-c:v", "libx264", "-preset", "ultrafast",
                    "-crf", "16", "-g", "30", "-c:a", "aac", str(EDITOR / "public" / rel)], check=True)
    props = carpeta / f"clip_{n:02}.props.json"
    props.write_text(json.dumps({
        "src": rel, "duracion": round(dur - 0.1, 2),
        "captions": json.loads(subs.read_text(encoding="utf-8")),
        "titulo": titulo, "cam": recorte(a.cam), "juego": recorte(a.juego),
    }, ensure_ascii=False), encoding="utf-8")
    try:
        subprocess.run(["npx", "remotion", "render", "ClipStream", str(carpeta / f"final_{n:02}.mp4"),
                        f"--props={props}", "--timeout=120000", "--concurrency=3"], cwd=EDITOR, check=True, shell=True)
    finally:
        (EDITOR / "public" / rel).unlink()  # public/ no se llena de clips


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("carpeta", nargs="?")
    ap.add_argument("--n", help="clips a montar, p. ej. 1,3 (por defecto todos)")
    ap.add_argument("--cam")
    ap.add_argument("--juego")
    ap.add_argument("--titulo")
    ap.add_argument("--remotion", action="store_true", help="montar con Remotion (más lento)")
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args()
    if a.test:
        return autotest()
    if not a.carpeta:
        ap.error("falta la carpeta del trabajo")

    carpeta = Path(a.carpeta).resolve()
    picos = {p["n"]: p for p in json.loads((carpeta / "picos.json").read_text(encoding="utf-8"))}
    nums = [int(n) for n in a.n.split(",")] if a.n else sorted(picos)

    for n in nums:
        clip = carpeta / f"clip_{n:02}.mp4"
        subs = carpeta / f"clip_{n:02}.captions.json"
        if not subs.exists():
            transcribir(clip, subs)
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    str(clip)], capture_output=True, text=True, check=True).stdout)
        titulo = a.titulo or picos.get(n, {}).get("titulo")
        if a.remotion:
            montar_remotion(carpeta, n, clip, dur, subs, titulo, a)
        else:
            ass_f = carpeta / f"clip_{n:02}.ass"
            ass_f.write_text(ass(json.loads(subs.read_text(encoding="utf-8")), titulo, a.cam), encoding="utf-8")
            montar_ffmpeg(clip, carpeta / f"final_{n:02}.mp4", dur - 0.1, ass_f, recorte(a.cam), recorte(a.juego))
        print("Listo:", carpeta / f"final_{n:02}.mp4")


def autotest():
    caps = [{"text": " hola", "startMs": 0, "endMs": 300}, {"text": " que", "startMs": 400, "endMs": 600},
            {"text": " tal", "startMs": 1000, "endMs": 1200}]
    assert [len(p) for p in paginas(caps)] == [2, 1]
    a = ass(caps, "Se le va", "0,0,.2,.3")
    assert "Dialogue: 0,0:00:00.00,0:00:00.40,Sub,,0,0,0,,{\\c&H003D8AFF}HOLA{\\c&H00DCE9F3} QUE" in a, a
    assert "0:00:01.00,0:00:01.20,Sub" in a and "SE LE VA" in a and t_ass(3723450) == "1:02:03.45"
    z = zona({"x": 0, "y": 0, "w": 1, "h": 1}, 1080, 820, 1920, 1080)
    assert z.startswith("crop=1422:1080:248:0"), z
    print("autotest ok")


if __name__ == "__main__":
    main()

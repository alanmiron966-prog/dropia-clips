"""Conexión con la cuenta de TikTok de quien usa la app y subida de los verticales (Content Posting API, Direct Post).
secrets/tiktok_app.json: {"client_key": "...", "client_secret": "..."} de la app en developers.tiktok.com,
con la redirect URI http://localhost:3455/callback/ dada de alta. secrets/tt_token.json: el permiso de cada usuario.
Mientras TikTok no audite la app, solo deja publicar en SELF_ONLY (solo tú) y con la cuenta en privado."""
import hashlib
import json
import secrets as azar
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

SECRETS = Path(__file__).resolve().parent / "secrets"
APP = SECRETS / "tiktok_app.json"
TOKEN = SECRETS / "tt_token.json"
API = "https://open.tiktokapis.com/v2/"
PUERTO = 3455
REDIRECT = f"http://localhost:{PUERTO}/callback/"
MB = 2 ** 20


def _app():
    return json.loads(APP.read_text(encoding="utf-8"))


def _guardar(r):
    t = r.json()
    if "access_token" not in t:
        raise RuntimeError(t.get("error_description") or t.get("error") or str(t))
    t["caduca"] = time.time() + t["expires_in"] - 60
    TOKEN.write_text(json.dumps(t), encoding="utf-8")
    return t


def token():
    """Token válido del usuario (lo renueva si caducó: dura 2 h; el de renovar, 180 días), o None sin conectar."""
    if not (APP.exists() and TOKEN.exists()):
        return None
    t = json.loads(TOKEN.read_text(encoding="utf-8"))
    if time.time() > t["caduca"]:
        a = _app()
        t = _guardar(requests.post(API + "oauth/token/", timeout=30, data={
            "client_key": a["client_key"], "client_secret": a["client_secret"],
            "grant_type": "refresh_token", "refresh_token": t["refresh_token"]}))
    return t["access_token"]


def login():
    """Abre el login de TikTok en el navegador y espera la vuelta en localhost (5 min como mucho)."""
    a = _app()
    verifier, estado, vuelta = azar.token_urlsafe(48), azar.token_urlsafe(16), {}

    class Vuelta(BaseHTTPRequestHandler):
        def do_GET(self):
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            if q.get("state") == estado:
                vuelta.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("Listo. Cierra esta pestaña y vuelve a DROPIA Clips.".encode())

        def log_message(self, *_):
            pass

    srv = HTTPServer(("localhost", PUERTO), Vuelta)
    srv.timeout = 5
    webbrowser.open("https://www.tiktok.com/v2/auth/authorize/?" + urlencode({
        "client_key": a["client_key"], "scope": "user.info.basic,video.publish", "response_type": "code",
        "redirect_uri": REDIRECT, "state": estado, "code_challenge_method": "S256",
        "code_challenge": hashlib.sha256(verifier.encode()).hexdigest()}))  # TikTok pide hex, no base64url
    fin = time.time() + 300
    while not vuelta and time.time() < fin:
        srv.handle_request()
    srv.server_close()
    if "code" not in vuelta:
        raise RuntimeError(vuelta.get("error_description") or "No se completó el login de TikTok.")
    _guardar(requests.post(API + "oauth/token/", timeout=30, data={
        "client_key": a["client_key"], "client_secret": a["client_secret"], "code": vuelta["code"],
        "grant_type": "authorization_code", "redirect_uri": REDIRECT, "code_verifier": verifier}))


def _post(ruta, cuerpo=None):
    r = requests.post(API + ruta, timeout=60, data=json.dumps(cuerpo) if cuerpo else None,
                      headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json; charset=UTF-8"})
    j = r.json()
    if j.get("error", {}).get("code", "ok") != "ok":
        raise RuntimeError(f"{j['error']['code']}: {j['error'].get('message', '')}")
    return j.get("data", {})


def creador():
    """Cuenta conectada y lo que deja hacer (privacidades, comentarios/dúo/stitch, duración máxima), o None."""
    return _post("post/publish/creator_info/query/") if token() else None


def trozos(tam):
    """(chunk_size, [(primer, último byte)]). Hasta 64 MB va entero; si no, trozos de 10 MB y el último se lleva el resto."""
    t = tam if tam <= 64 * MB else 10 * MB
    n = tam // t
    return t, [(i * t, tam - 1 if i == n - 1 else (i + 1) * t - 1) for i in range(n)]


def subir(video, titulo, privacidad, comentar, duo, stitch, marca_propia=False, marca_ajena=False):
    """Inicia la publicación, sube el archivo y devuelve el publish_id (TikTok lo procesa después)."""
    tam = Path(video).stat().st_size
    t, partes = trozos(tam)
    d = _post("post/publish/video/init/", {
        "post_info": {"title": titulo, "privacy_level": privacidad, "disable_comment": not comentar,
                      "disable_duet": not duo, "disable_stitch": not stitch,
                      "brand_organic_toggle": marca_propia, "brand_content_toggle": marca_ajena},
        "source_info": {"source": "FILE_UPLOAD", "video_size": tam, "chunk_size": t, "total_chunk_count": len(partes)}})
    with open(video, "rb") as f:
        for a, b in partes:  # en orden, como pide TikTok
            f.seek(a)
            requests.put(d["upload_url"], data=f.read(b - a + 1), timeout=600,
                         headers={"Content-Type": "video/mp4", "Content-Range": f"bytes {a}-{b}/{tam}"}).raise_for_status()
    return d["publish_id"]


def estado(publish_id):
    """status: PROCESSING_UPLOAD, PROCESSING_DOWNLOAD, SEND_TO_USER_INBOX, PUBLISH_COMPLETE o FAILED (con fail_reason)."""
    return _post("post/publish/status/fetch/", {"publish_id": publish_id})

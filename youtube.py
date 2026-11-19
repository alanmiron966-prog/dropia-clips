"""Conexión con el canal de YouTube de quien usa la app y subida de los verticales.
secrets/client_secret.json es el de la app (Google Cloud); secrets/yt_token.json, el permiso de cada usuario.
Mientras Google no verifique la app, YouTube deja en privado lo que se sube por la API."""
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from datetime import date
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SECRETS = Path(__file__).resolve().parent / "secrets"
TOKEN = SECRETS / "yt_token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
INGRESOS = "https://www.googleapis.com/auth/yt-analytics-monetary.readonly"  # ver ingresos (y vistas) de YouTube Analytics


def creds(abrir_login=False):
    c = Credentials.from_authorized_user_file(TOKEN) if TOKEN.exists() else None  # con los permisos que dio el usuario
    if c and c.expired and c.refresh_token:
        c.refresh(Request())
    elif not c or not c.valid:
        if not abrir_login:
            return None
        c = login()
    TOKEN.write_text(c.to_json(), encoding="utf-8")
    return c


def login():
    """Abre el login de Google pidiendo subir videos, leer el canal y ver los ingresos."""
    c = InstalledAppFlow.from_client_secrets_file(SECRETS / "client_secret.json", SCOPES + [INGRESOS]).run_local_server(port=0)
    TOKEN.write_text(c.to_json(), encoding="utf-8")
    return c


def resultados(ids):
    """Vistas, me gusta y comentarios de cada video; ingresos si el canal dio permiso y YouTube le paga.
    Los ingresos de YouTube llegan con unos 2 días de retraso."""
    c = creds()
    if not c or not ids:
        return {"videos": {}, "ingresos": None, "aviso_ingresos": None}
    yt = build("youtube", "v3", credentials=c)
    videos = {}
    for i in range(0, len(ids), 50):
        for v in yt.videos().list(part="statistics,snippet", id=",".join(ids[i:i + 50])).execute()["items"]:
            st = v["statistics"]
            videos[v["id"]] = {"vistas": int(st.get("viewCount", 0)), "megusta": int(st.get("likeCount", 0)),
                               "comentarios": int(st.get("commentCount", 0)), "fecha": v["snippet"]["publishedAt"][:10]}
    if INGRESOS not in (c.scopes or []):
        return {"videos": videos, "ingresos": None, "aviso_ingresos": "permiso"}
    try:
        r = build("youtubeAnalytics", "v2", credentials=c).reports().query(
            ids="channel==MINE", startDate="2020-01-01", endDate=date.today().isoformat(),
            metrics="estimatedRevenue", dimensions="video", filters="video==" + ",".join(ids[:200]), currency="USD").execute()
        ingresos = {fila[0]: fila[1] for fila in r.get("rows", [])}
        for v, d in videos.items():
            d["ingresos"] = round(ingresos.get(v, 0.0), 2)
        return {"videos": videos, "ingresos": round(sum(ingresos.values()), 2), "aviso_ingresos": None}
    except Exception as e:  # p. ej. canal sin monetizar: YouTube no da ingresos
        return {"videos": videos, "ingresos": None, "aviso_ingresos": f"YouTube no da ingresos de este canal: {e}"[:300]}


def canal(abrir_login=False):
    """Nombre del canal conectado, o None. Con abrir_login abre el navegador para conectar uno."""
    c = creds(abrir_login)
    if not c:
        return None
    return build("youtube", "v3", credentials=c).channels().list(part="snippet", mine=True).execute()["items"][0]["snippet"]["title"]


def subir(video, titulo, descripcion="", tags=()):
    """Sube el vertical como Short público. Devuelve (url, privacidad que dejó YouTube)."""
    yt = build("youtube", "v3", credentials=creds())
    body = {"snippet": {"title": titulo[:100], "description": descripcion, "tags": list(tags), "categoryId": "20"},  # 20 = Gaming
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(video), chunksize=-1, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return f"https://youtube.com/shorts/{resp['id']}", resp["status"]["privacyStatus"]

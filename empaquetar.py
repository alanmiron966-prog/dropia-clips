"""Crea dist/DROPIA-Clips.zip para repartir: el código, el instalador, fuentes, bin (YouTube y TikTok se suben por sus Studio).
Fuera queda todo lo personal: tus videos (trabajo/), tus cuentas (tokens), tu app de TikTok, tus cámaras y las copias."""
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DENTRO = ["app.py", "chat_clips.py", "montar.py", "youtube.py", "tiktok.py", "ui.html", "dropia.ico",
          "instalar.bat", "instalar.py", "requirements.txt", "README.md"]
CARPETAS = ["fuentes", "bin", "guia"]
# client_secret.json fuera: sin verificar por Google, a cualquiera que no seas tú le sale "acceso bloqueado".
PERSONAL = ("yt_token.json", "tt_token.json", "tiktok_app.json", "client_secret.json")


def archivos():
    yield from (ROOT / f for f in DENTRO)
    for c in CARPETAS:
        yield from (f for f in (ROOT / c).rglob("*") if f.is_file() and f.suffix != ".pdb")


def main():
    zipf = ROOT / "dist" / "DROPIA-Clips.zip"
    zipf.parent.mkdir(exist_ok=True)
    lista = list(archivos())
    faltan = [f.relative_to(ROOT) for f in lista if not f.exists()]
    assert not faltan, f"faltan: {faltan}"
    assert not any(f.name in PERSONAL for f in lista), "se colaba un token personal"
    with zipfile.ZipFile(zipf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in lista:
            z.write(f, Path("DROPIA Clips") / f.relative_to(ROOT))
    print(f"{zipf}  ({zipf.stat().st_size / 1e6:.1f} MB, {len(lista)} archivos)")


if __name__ == "__main__":
    main()

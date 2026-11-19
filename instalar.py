"""Instalador de DROPIA Clips: librerías de Python, ffmpeg y Node.js (con winget) y el acceso directo del escritorio.
Lo lanza instalar.bat. Se puede repetir sin miedo: lo que ya está no se toca."""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROGRAMAS = {"ffmpeg": "Gyan.FFmpeg", "node": "OpenJS.NodeJS.LTS"}  # comando -> paquete de winget


def paso(txt):
    print(f"\n  > {txt}")


def main():
    paso("Librerías de Python")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--upgrade", "-r", str(ROOT / "requirements.txt")], check=True)
    for cmd, paquete in PROGRAMAS.items():
        if shutil.which(cmd):
            paso(f"{cmd}: ya instalado")
            continue
        paso(f"Instalando {cmd}")
        subprocess.run(["winget", "install", "-e", "--id", paquete, "--accept-package-agreements", "--accept-source-agreements"])
    paso("Acceso directo en el escritorio")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    escritorio = subprocess.run(["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
                                capture_output=True, text=True).stdout.strip()
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{Path(escritorio) / 'DROPIA Clips.lnk'}');"
          f"$s.TargetPath='{pythonw}';$s.Arguments='\"{ROOT / 'app.py'}\"';$s.WorkingDirectory='{ROOT}';"
          f"$s.IconLocation='{ROOT / 'dropia.ico'}';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    print("\n  Listo. Abre DROPIA Clips desde el escritorio. Si ffmpeg o Node se acaban de instalar, reinicia el PC antes.\n")


if __name__ == "__main__":
    main()

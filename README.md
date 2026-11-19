# dropia-clips

Saca clips de directos de GTA (Twitch y YouTube) usando el chat como detector de momentos.

## Instalar

Doble clic en `instalar.bat`: instala Python (si falta), las librerías de `requirements.txt`, ffmpeg y Node.js con winget,
y crea el acceso directo **DROPIA Clips** en el escritorio. Se puede repetir sin problema.

## App (lo normal)

Doble clic en **DROPIA Clips** del escritorio. Se abre en su propia ventana (Chrome o Edge en modo app; si no hay, en el navegador).
**Ajustes**: conectar o desconectar YouTube, ver si están ffmpeg, yt-dlp y Node, y actualizar yt-dlp.
**eliminar** (en cada directo) o "Eliminar los publicados" (pestaña Publicados) borra los videos del PC; si algo se publicó,
se guardan solo sus `.json` para que siga en Resultados.

1. Pega el enlace (directo de Twitch, o video o directo de YouTube) y pulsa Empezar.
   Sin chat (video normal de YouTube) usa "lo más visto" del video y, si YouTube no lo da, los tramos con más volumen.
2. Sola: mira el canal, lee el chat, baja los 8 mejores momentos y monta en vertical los 3 con más puntos.
3. La primera vez de cada canal pide arrastrar un rectángulo sobre la webcam (o "No tiene cámara"); queda en `canales.json`.
4. Cada momento se puede montar o rehacer con su título, verlo y descargar el vertical.

Cerrar la pestaña no apaga la app; sigue montando en segundo plano. Errores en `trabajo/<id>/log.txt`.

## Uso por consola

```
python chat_clips.py URL_DEL_DIRECTO            # 8 mejores momentos, clips de 45 s
python chat_clips.py URL --top 5 --dur 30       # otros valores
python chat_clips.py URL --solo-picos           # solo la lista, sin bajar video
python chat_clips.py --test                     # autotest
```

Resultado en `trabajo/<id>/`: `picos.json` (minuto, puntos y mensajes del chat de cada pico) y `clip_01.mp4`...

Luego, montar los elegidos en vertical 9:16 con subtítulos (ffmpeg, fuente Anton en `fuentes/`; con `--remotion` usa el proyecto `reels-editor`, más lento):

```
python montar.py trabajo/<id> --n 2,5 --cam 0.02,0.025,0.19,0.33 --titulo "LO ESTAFAN"
python montar.py trabajo/<id> --n 1                # sin --cam: video entero sobre fondo desenfocado
```

- `--cam x,y,w,h`: zona de la webcam en fracciones del video (0-1). Cámara arriba, juego abajo.
- `--juego x,y,w,h`: zona del juego a mostrar abajo (por defecto, el 60 % central).
- Sale `final_NN.mp4` (1080x1920, audio original, subtítulos palabra a palabra). Con ffmpeg, menos de 1 min por clip de 45 s (Whisper ~20 s + montaje ~10 s); con Remotion, 4-5 min. La app empieza a montar el mejor clip en cuanto está bajado, sin esperar al resto.
- Los subtítulos quedan en `clip_NN.captions.json`: si Whisper se equivoca, se corrige ahí y se vuelve a montar.

## Cómo decide

1. Baja el chat entero (YouTube con yt-dlp; Twitch con TwitchDownloaderCLI en `bin/`).
2. Cuenta mensajes cada 10 s. Las risas (jaja, jsjs, xd, KEKW, 😂) valen doble.
3. Ignora los primeros 5 min (saludos) y elige los picos más altos, separados entre sí.
4. Cada clip empieza 20 s antes del pico, porque el chat reacciona tarde.

## Pendiente

- Voz de ElevenLabs encima del clip (narrado).
- Probar con un VOD de GTA RP de Twitch de los streamers elegidos.
- Publicar: `youtube DROPIA/tools/yt_upload.py` (YouTube) y Chrome (TikTok).

Solo con streamers que permitan recortar sus directos.

## Publicar en YouTube

Sin conectar nada: **Subir a YouTube** copia título y hashtags, abre YouTube Studio y señala el vertical en el Explorador
para arrastrarlo. Luego "Ya lo publiqué en YouTube" lo pasa a Publicados. La versión repartida (`empaquetar.py`) no lleva
`client_secret.json`, así que los demás usuarios solo tienen este camino.

Con la llave de Google (solo tú):
Arriba sale el canal conectado ("Conectar mi canal" abre el login de Google). En cada vertical: escribe el título y pulsa
**Publicar en YouTube**; sube como Short público y deja el enlace en la tarjeta (se guarda en `trabajo/<id>/publicado.json`).
Mientras Google no verifique la app, YouTube lo deja en privado.

`secrets/` es privado: `yt_token.json` es el permiso de TU canal y nunca va en la versión que se vende.
TikTok (`tiktok.py`, Content Posting API): crea una app en developers.tiktok.com con Login Kit (Desktop) y Content Posting
(Direct Post, scope `video.publish`), da de alta la redirect URI `http://localhost:3455/callback/` y guarda
`secrets/tiktok_app.json` con `{"client_key": "...", "client_secret": "..."}`. `secrets/tt_token.json` es el permiso de TU cuenta
y tampoco va en la versión que se vende. Hasta que TikTok audite la app solo publica en "Solo yo" y con la cuenta en privado.

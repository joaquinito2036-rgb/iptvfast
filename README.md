# IPTVFast

Generador automatizado de listas M3U, JSON y XMLTV para canales FAST/IPTV.

## Qué genera

- `output/all.m3u`: todos los canales en una sola lista.
- `output/all.json.gz`: catálogo completo en JSON comprimido.
- `output/[platform]_all.m3u`: lista por plataforma.
- `output/[platform]_[country].m3u`: lista por plataforma y país, cuando se puede detectar país.
- `output/xmltv.xml.gz`: XMLTV comprimido desde EPGShare.
- `output/summary.json`: resumen de ejecución.
- `output/manifest.json`: índice de archivos generados.

## Automatización

El workflow de GitHub Actions se ejecuta cada 6 horas y también manualmente:

```yaml
cron: "0 */6 * * *"
```

Publica los resultados como artefacto de Actions y, opcionalmente, puede hacer commit al propio repo si activas `COMMIT_OUTPUTS=true`.

## Uso local

```bash
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

pip install -r requirements.txt
python -m iptvfast.generate
```

## DRM

El proyecto **no rompe ni evita DRM**. Si una fuente pública ya declara metadatos como `drm_license`, `license_url`, `key_system`, `clearkey`, etc., el generador los conserva en el JSON y añade propiedades compatibles con reproductores tipo Kodi/InputStream en la M3U.

## Redirecciones Matt Huisman / jmp2.uk

El generador intenta resolver, mediante `HEAD` o `GET` sin descargar el vídeo completo, redirecciones de este tipo:

- `https://jmp2.uk/plu-*.m3u8`
- `https://jmp2.uk/rok-*.m3u8`
- `https://jmp2.uk/plex-*.m3u8`
- `https://jmp2.uk/stvp-*`
- `https://i.mjh.nz/.r/*.m3u8`

La URL original se conserva en JSON como `original_url` y la resuelta como `url`.

## Aviso

Muchas fuentes pueden estar geobloqueadas, caídas o cambiar sin previo aviso. Este repositorio solo agrega URLs públicas indicadas en la configuración.

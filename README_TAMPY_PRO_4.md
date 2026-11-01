# TAMPY PRO 4.0 — Studio

Nueva rama experimental construida sobre la versión estable de TAMPY. No reemplaza la instalación estable.

## Nuevas capacidades
- Chat + memoria PostgreSQL/Supabase.
- Análisis local de XLSX, XLSM, XLS y CSV con Python.
- Análisis de múltiples archivos.
- Detección automática de posibles campos: producto, proveedor, sucursal, categoría, ventas, compras, costo, precio, stock, cantidad, margen y fecha.
- Dashboard HTML autónomo por archivo.
- Dashboard HTML multiarchivo.
- Exportación TXT, Word, PDF, Excel y HTML.
- Generador de Landing Pages HTML autónomas usando Groq como motor de diseño.
- Descarga real de archivos, no solo código mostrado en pantalla.
- Compatibilidad con PDF, DOCX y PPTX para análisis de contenido.

## Variables de entorno
Mantén las mismas variables de la versión estable:
- GROQ_API_KEY
- DATABASE_URL
- TAMPY_CLAVE (opcional)
- TAMPY_DATA_DIR (opcional)
- MODELO_TEXTO (opcional)
- MODELO_VISION (opcional)

## Deploy recomendado
1. Crea un repositorio/branch separado para esta versión.
2. En Render crea un Web Service independiente para pruebas.
3. Configura las mismas variables de entorno que TAMPY estable.
4. Usa `pip install -r requirements.txt` como build.
5. Usa `gunicorn app:app` como start command.
6. Prueba `/salud`, `/diagnostico`, chat, memoria, Excel, dashboard y Landing Page antes de sustituir producción.

## Nota sobre almacenamiento
La memoria está diseñada para persistir en PostgreSQL/Supabase. Los archivos subidos a `TAMPY_DATA_DIR` son temporales en Render Free; esta rama todavía no incluye almacenamiento persistente de archivos. La siguiente fase puede incorporar una biblioteca persistente de proyectos/archivos.

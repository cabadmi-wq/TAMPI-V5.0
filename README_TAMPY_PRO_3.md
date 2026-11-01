# TAMPY PRO 3.0

Esta versión parte de la versión estable de TAMPY y añade un motor local de archivos/artefactos para reducir el consumo de la API de IA.

## Nuevas funciones

- Análisis local de XLSX/XLSM/XLS/CSV con Python/openpyxl/xlrd.
- Resumen compacto del archivo para la IA; no se envía el libro completo a Groq.
- Soporte de PDF, DOCX, PPTX y archivos de texto.
- Generación de dashboard HTML autónomo desde Excel/CSV.
- Descarga directa de `*_dashboard.html`.
- Descarga de `*_dashboard.zip`.
- Exportación de respuestas a TXT, Word, PDF, Excel y HTML.
- Manejo de errores 429 de Groq sin perder la conversación.
- Mantiene PostgreSQL/Supabase, memoria e historial de TAMPY 2.0.
- Archivos temporales se guardan en `TAMPY_DATA_DIR/uploads` durante la vida de la instancia.

## Proceso recomendado

1. Subir archivo.
2. TAMPY lo analiza localmente.
3. Para Excel/CSV se calculan filas, columnas, métricas y categorías con Python.
4. Solo se envía a Groq un contexto compacto cuando hace falta interpretación.
5. Puedes pulsar 📊 para generar el dashboard HTML o 📦 para el ZIP.
6. Las respuestas de TAMPY muestran botones de descarga TXT/Word/PDF/Excel/HTML.

## Render

Root Directory:
`TAMPY_PRO_3_0` si el repositorio contiene esta carpeta, o vacío si sus archivos están directamente en la raíz.

Build:
`pip install -r requirements.txt`

Start:
`gunicorn app:app`

Variables existentes:
- GROQ_API_KEY
- TAMPY_CLAVE
- DATABASE_URL
- MODELO_TEXTO
- MODELO_VISION

No necesitas un Persistent Disk para la memoria: la memoria usa PostgreSQL/Supabase.

## Nota sobre almacenamiento de archivos

Los archivos subidos y temporales se guardan en el filesystem de la instancia. En Render Free ese filesystem es efímero. El dashboard se genera en memoria y se descarga inmediatamente. Para una biblioteca permanente de archivos habrá que conectar Supabase Storage en una siguiente fase.

## Siguiente fase recomendada

- Supabase Storage para biblioteca permanente.
- RAG/vectorización.
- Router de modelos.
- Generación de dashboards más específicos por compras/ventas.
- autenticación multiusuario.

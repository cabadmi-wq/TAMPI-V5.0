# TAMPY PRO 4.1 — Archivos corregibles y descargables

Esta versión conserva la base de TAMPY PRO 4.0 y agrega un flujo para que TAMPY pueda **recibir un archivo, interpretar una instrucción en lenguaje natural, aplicar una transformación segura con Python y devolver el archivo corregido/modificado como descarga**.

## Ejemplos
- “Elimina los registros duplicados y devuelve el Excel corregido.”
- “Quita las filas donde COSTO sea negativo.”
- “Reemplaza ‘NORTE’ por ‘NORTE QUITO’ en la columna SUCURSAL.”
- “Completa los vacíos de PROVEEDOR con ‘SIN PROVEEDOR’.”
- “Agrega una columna ESTADO con valor PENDIENTE.”
- “Ordena por TOTAL de mayor a menor.”
- “Limpia los espacios sobrantes de DESCRIPCION.”

## Formatos de procesamiento
- XLSX
- XLSM (conserva la extensión macro-enabled, aunque no ejecuta macros)
- XLS (se devuelve como XLSX limpio)
- CSV
- TXT/MD/JSON con operaciones de limpieza sencillas

## Importante
El modelo no ejecuta código generado por el usuario. Primero convierte la instrucción a un plan estructurado de operaciones permitidas y Python aplica únicamente esas operaciones. Esto reduce el riesgo de modificar un archivo de forma arbitraria.

Para Excel se procesa sobre la primera fila como encabezado. Si el libro tiene encabezados especiales de dos filas, conviene indicarlo en la instrucción o adaptar el plan de procesamiento.

La memoria de TAMPY continúa funcionando con PostgreSQL/Supabase si `DATABASE_URL` está configurada.

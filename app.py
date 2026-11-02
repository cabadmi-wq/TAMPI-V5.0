import hashlib
import csv
import statistics
import hmac
import io
import json
import logging
import os
import re
import sqlite3
import uuid
import copy

from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file
from groq import Groq

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tampy")

app = Flask(__name__)
LIMITE_MB = 100
MAX_TOKENS_RESPUESTA = int(os.environ.get('MAX_TOKENS', '6000'))
app.config["MAX_CONTENT_LENGTH"] = LIMITE_MB * 1024 * 1024

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
CLAVE_ACCESO = os.environ.get("TAMPY_CLAVE", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = False
DATA_DIR = Path(os.environ.get("TAMPY_DATA_DIR", str(Path(__file__).parent / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR = DATA_DIR / "exports"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

MODELOS_TEXTO = [m for m in [
    os.environ.get("MODELO_TEXTO"),
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
] if m]
MODELOS_VISION = [m for m in [
    os.environ.get("MODELO_VISION"),
    "qwen/qwen3.8-27b",
] if m]

SYSTEM_BASE = (
    "Eres TAMPY, un asistente de IA profesional, claro y amable. "
    "Respondes siempre en español, salvo que el usuario pida otro idioma. "
    "Usa Markdown cuando ayude. No inventes información. "
    "Tienes memoria de largo plazo proporcionada por el sistema: úsala solo cuando sea relevante. "
    "Si una memoria contradice el mensaje actual del usuario, prioriza el mensaje actual. "
    "ARCHIVOS: la aplicación entrega archivos descargables automáticamente, como tarjetas. "
    "Cuando el usuario pida un archivo (Excel, Word, PDF, PowerPoint, HTML, Python, JS, CSS, JSON, CSV, SQL, TXT, etc.) "
    "o te pida CREAR algo que en la práctica es un archivo (una mini app, página web, landing, script, programa, herramienta, juego, "
    "calculadora, sistema, reporte, documento, hoja de cálculo, presentación o tabla de datos), "
    "entrega SOLO ese archivo dentro de un único bloque de código cuya primera línea sea ```archivo:nombre.ext "
    "con la extensión correcta (por ejemplo ```archivo:ventas.xlsx, ```archivo:informe.docx, ```archivo:informe.pdf, "
    "```archivo:presentacion.pptx, ```archivo:index.html, ```archivo:script.py). "
    "Reglas por formato: HTML, Python, JS, CSS, JSON, CSV, SQL y TXT: contenido literal y COMPLETO, sin recortar ni usar '...'. "
    "Excel (.xlsx): tablas Markdown con fila de encabezados; una hoja por cada título ## que preceda a una tabla; "
    "puedes usar fórmulas como =SUM(B2:B10). "
    "Word (.docx) y PDF: texto en Markdown con títulos (#, ##), listas, **negritas** y tablas Markdown. "
    "PowerPoint (.pptx): un título ## por diapositiva seguido de viñetas. "
    "Una mini app o página sin otra indicación = UN solo archivo HTML autocontenido (CSS y JS dentro) que funcione al abrirlo en el celular. "
    "Usa nombres descriptivos en minúsculas con guiones bajos (control_gastos.html, inventario_enero.xlsx). "
    "Nunca pegues el código ni los datos completos en el chat: todo va dentro del bloque archivo. "
    "Antes del bloque escribe como máximo una frase breve; no repitas el contenido fuera del bloque ni menciones botones. "
    "Si el usuario adjuntó un Excel o CSV y pide corregirlo o modificarlo, NO generes tú el archivo: la aplicación lo procesa "
    "automáticamente; solo explica en una o dos frases qué cambio se aplicará. "
    "Si el usuario no pide un archivo, responde normalmente sin usar ```archivo. "
    "Nunca digas que no puedes crear archivos."
)
MAX_MENSAJES = 16
MAX_CARACTERES = 70000
MAX_TEXTO_ARCHIVO = 16000
MAX_FILAS_DASHBOARD = 20000
MAX_FILAS_ANALISIS = 100000
MAX_FILAS_PROCESAMIENTO = 250000
EXT_TEXTO = {"txt","md","csv","tsv","json","xml","html","htm","css","js","ts","py","java","c","cpp","cs","php","rb","go","rs","sql","yaml","yml","ini","log","sh","bat"}


def now():
    return datetime.now(timezone.utc).isoformat()


def profile_id():
    if not CLAVE_ACCESO:
        # For a public/no-key deployment, use a browser supplied id.
        return request.headers.get("X-Tampy-Profile", "public")[:120]
    return hashlib.sha256(CLAVE_ACCESO.encode()).hexdigest()[:32]


def autorizado():
    if not CLAVE_ACCESO:
        return True
    return hmac.compare_digest(request.headers.get("X-Clave", ""), CLAVE_ACCESO)


def db_kind():
    return "postgres" if USE_POSTGRES else "sqlite"


def sqlite_conn():
    c = sqlite3.connect(DATA_DIR / "tampy.db")
    c.row_factory = sqlite3.Row
    return c


def pg_conn():
    import psycopg
    from psycopg.rows import dict_row
    url = DATABASE_URL
    if url and "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return psycopg.connect(url, autocommit=True, connect_timeout=10, row_factory=dict_row)


def init_db():
    if USE_POSTGRES:
        with pg_conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, title TEXT NOT NULL, messages JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, content TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'general', created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_conv_profile ON conversations(profile_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_mem_profile ON memories(profile_id)")
    else:
        with sqlite_conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, title TEXT NOT NULL, messages TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, content TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'general', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_conv_profile ON conversations(profile_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_mem_profile ON memories(profile_id)")


try:
    if DATABASE_URL:
        try:
            init_db()
            USE_POSTGRES = True
            log.info("Memoria: PostgreSQL/Supabase activo")
        except Exception:
            USE_POSTGRES = False
            log.exception("No se pudo conectar a PostgreSQL/Supabase; se usará SQLite local como respaldo")
            init_db()
    else:
        init_db()
        log.info("Memoria: SQLite local activo")
except Exception:
    log.exception("No se pudo inicializar ninguna base de datos")


def save_conversation(cid, title, messages):
    pid = profile_id(); ts = now(); payload = json.dumps(messages, ensure_ascii=False)
    if USE_POSTGRES:
        with pg_conn() as c:
            exists = c.execute("SELECT id FROM conversations WHERE id=%s AND profile_id=%s", (cid,pid)).fetchone()
            if exists:
                c.execute("UPDATE conversations SET title=%s,messages=%s,updated_at=%s WHERE id=%s AND profile_id=%s", (title,payload,ts,cid,pid))
            else:
                c.execute("INSERT INTO conversations(id,profile_id,title,messages,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s)", (cid,pid,title,payload,ts,ts))
    else:
        with sqlite_conn() as c:
            c.execute("INSERT OR REPLACE INTO conversations(id,profile_id,title,messages,created_at,updated_at) VALUES(?,?,?,?,COALESCE((SELECT created_at FROM conversations WHERE id=?),?),?)", (cid,pid,title,payload,cid,ts,ts))


def list_conversations():
    pid=profile_id()
    if USE_POSTGRES:
        with pg_conn() as c:
            rows=c.execute("SELECT id,title,created_at,updated_at FROM conversations WHERE profile_id=%s ORDER BY updated_at DESC LIMIT 100",(pid,)).fetchall()
            return [dict(r) for r in rows]
    with sqlite_conn() as c:
        rows=c.execute("SELECT id,title,created_at,updated_at FROM conversations WHERE profile_id=? ORDER BY updated_at DESC LIMIT 100",(pid,)).fetchall()
        return [dict(r) for r in rows]


def get_conversation(cid):
    pid=profile_id()
    if USE_POSTGRES:
        with pg_conn() as c: r=c.execute("SELECT id,title,messages,created_at,updated_at FROM conversations WHERE id=%s AND profile_id=%s",(cid,pid)).fetchone()
    else:
        with sqlite_conn() as c: r=c.execute("SELECT id,title,messages,created_at,updated_at FROM conversations WHERE id=? AND profile_id=?",(cid,pid)).fetchone()
    if not r: return None
    d=dict(r); d["messages"]=json.loads(d["messages"]) if isinstance(d["messages"],str) else d["messages"]; return d


def delete_conversation(cid):
    pid=profile_id()
    if USE_POSTGRES:
        with pg_conn() as c: c.execute("DELETE FROM conversations WHERE id=%s AND profile_id=%s",(cid,pid))
    else:
        with sqlite_conn() as c: c.execute("DELETE FROM conversations WHERE id=? AND profile_id=?",(cid,pid))


def add_memory(content, category="general"):
    mid=str(uuid.uuid4()); ts=now(); pid=profile_id()
    if USE_POSTGRES:
        with pg_conn() as c: c.execute("INSERT INTO memories(id,profile_id,content,category,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s)",(mid,pid,content,category,ts,ts))
    else:
        with sqlite_conn() as c: c.execute("INSERT INTO memories VALUES(?,?,?,?,?,?)",(mid,pid,content,category,ts,ts))
    return mid


def list_memories():
    pid=profile_id()
    if USE_POSTGRES:
        with pg_conn() as c: rows=c.execute("SELECT id,content,category,created_at FROM memories WHERE profile_id=%s ORDER BY updated_at DESC LIMIT 200",(pid,)).fetchall()
    else:
        with sqlite_conn() as c: rows=c.execute("SELECT id,content,category,created_at FROM memories WHERE profile_id=? ORDER BY updated_at DESC LIMIT 200",(pid,)).fetchall()
    return [dict(r) for r in rows]


def delete_memory(mid):
    pid=profile_id()
    if USE_POSTGRES:
        with pg_conn() as c: c.execute("DELETE FROM memories WHERE id=%s AND profile_id=%s",(mid,pid))
    else:
        with sqlite_conn() as c: c.execute("DELETE FROM memories WHERE id=? AND profile_id=?",(mid,pid))


def extract_memory_candidates(user_text):
    # Conservative explicit-memory syntax. User can say: "Recuerda que ..." or "Recuerda: ..."
    m=re.search(r"\brecuerda(?: que|:)?\s+(.+)", user_text, re.I|re.S)
    if not m: return []
    content=re.sub(r"\s+"," ",m.group(1)).strip()
    if 8 <= len(content) <= 800:
        return [(content,"usuario")]
    return []


def memory_context():
    try:
        mems=list_memories()
    except Exception:
        log.exception("No se pudo leer la memoria; continúo sin memoria en esta petición")
        return ""
    if not mems: return ""
    return "\n\nMEMORIA DEL USUARIO (solo usa lo relevante):\n" + "\n".join(f"- [{m['category']}] {m['content']}" for m in mems[:80])


def _valor_texto(v):
    if v is None: return ""
    if isinstance(v, float) and v.is_integer(): return str(int(v))
    return str(v).strip()

_NUM_RE = re.compile(r'^[\s$]*[-+]?[\d,]*\.?\d+(?:[eE][-+]?\d+)?\s*$')

def _a_numero(v):
    """Convierte una celda a número (o None) sin lanzar excepciones costosas."""
    if v is None or isinstance(v, bool): return None
    if isinstance(v, (int, float)): return float(v)
    if isinstance(v, str) and _NUM_RE.match(v):
        try: return float(v.replace(",", "").replace("$", "").strip())
        except Exception: return None
    return None

def analizar_excel(nombre, datos):
    """Analiza XLSX/XLSM en UNA sola pasada (streaming): memoria casi constante, apto para archivos grandes.
    Devuelve la misma estructura que antes."""
    from openpyxl import load_workbook
    libro = load_workbook(io.BytesIO(datos), read_only=True, data_only=True, keep_links=False)
    hojas = []
    contexto = [f"ARCHIVO EXCEL: {nombre}", f"HOJAS: {len(libro.worksheets)}"]
    total_filas = 0
    for hoja in list(libro.worksheets)[:20]:
        headers = []; first_len = 0; maxlen = 0; nonempty = 0; ndata = 0
        muestra = []; stats = {}; freq = {}; overflow = set()
        for i, fila in enumerate(hoja.iter_rows(values_only=True)):
            vals = [_valor_texto(v) for v in fila]
            if not any(vals):
                continue
            nonempty += 1
            maxlen = max(maxlen, len(vals))
            if nonempty == 1:
                first_len = len(vals)
            if i == 0:
                headers = vals
            else:
                ndata += 1
                if len(muestra) < 30: muestra.append(vals)
                for j, raw in enumerate(fila):
                    n = _a_numero(raw)
                    if n is not None:
                        st = stats.get(j)
                        if st is None: stats[j] = [1, n, n, n]
                        else:
                            st[0] += 1; st[1] += n
                            if n < st[2]: st[2] = n
                            if n > st[3]: st[3] = n
                for j in range(min(len(vals), 30)):
                    v = vals[j]
                    if v == "" or j in overflow: continue
                    f = freq.setdefault(j, {})
                    f[v] = f.get(v, 0) + 1
                    if len(f) > 100:
                        overflow.add(j); del freq[j]
            if nonempty >= MAX_FILAS_ANALISIS:
                break
        if not headers and nonempty:
            headers = [f"Columna {k+1}" for k in range(first_len)]
        ncols = max(len(headers), maxlen, 0)
        headers = (headers + [f"Columna {k+1}" for k in range(ncols-len(headers))])[:ncols]
        numeric = []
        for j in range(ncols):
            st = stats.get(j)
            if st and st[0] >= max(3, int(ndata*0.5)):
                numeric.append({"column": headers[j] or f"Columna {j+1}", "count": st[0], "sum": round(st[1],2),
                                "avg": round(st[1]/st[0],2), "min": round(st[2],2), "max": round(st[3],2)})
        categorical = []
        for j in range(min(ncols, 30)):
            f = freq.get(j)
            if not f: continue
            nv = sum(f.values())
            if len(f) <= min(100, max(10, int(nv*.5))):
                top = sorted(f.items(), key=lambda x: x[1], reverse=True)[:10]
                categorical.append({"column": headers[j] or f"Columna {j+1}", "unique": len(f), "top": [{"value": str(k), "count": v} for k, v in top]})
        muestra = [dict(zip(headers, r + [""]*(ncols-len(r)))) for r in muestra]
        hoja_info = {"name": hoja.title, "rows": ndata, "columns": ncols, "headers": headers, "numeric": numeric[:15], "categorical": categorical[:15], "sample": muestra}
        hojas.append(hoja_info)
        total_filas += ndata
        contexto.append(f"Hoja '{hoja.title}': {ndata} filas, {ncols} columnas.")
        if numeric:
            contexto.append("  Métricas: " + "; ".join(f"{x['column']} suma={x['sum']} promedio={x['avg']} min={x['min']} max={x['max']}" for x in numeric[:8]))
        if categorical:
            contexto.append("  Categorías: " + "; ".join(f"{x['column']} únicos={x['unique']}" for x in categorical[:6]))
    try: libro.close()
    except Exception: pass
    contexto.append(f"TOTAL FILAS DE DATOS: {total_filas}")
    return {
        "tipo":"excel","nombre":nombre,"hojas":hojas,"total_filas":total_filas,
        "total_hojas":len(hojas),"contexto":"\n".join(contexto)
    }

def analizar_csv(nombre, datos):
    text=datos.decode("utf-8-sig",errors="replace")
    sample=text.splitlines()[:5000]
    dialect=csv.Sniffer().sniff("\n".join(sample[:20])) if len(sample)>1 else csv.excel
    reader=list(csv.reader(sample,dialect))
    headers=reader[0] if reader else []
    rows=reader[1:] if reader else []
    numeric=[]
    for j,h in enumerate(headers[:50]):
        nums=[]
        for r in rows:
            if j<len(r):
                try: nums.append(float(r[j].replace(",","").replace("$","").strip()))
                except: pass
        if len(nums)>=3:
            numeric.append({"column":h,"count":len(nums),"sum":round(sum(nums),2),"avg":round(sum(nums)/len(nums),2),"min":round(min(nums),2),"max":round(max(nums),2)})
    return {"tipo":"csv","nombre":nombre,"hojas":[{"name":"CSV","rows":len(rows),"columns":len(headers),"headers":headers,"numeric":numeric,"categorical":[],"sample":[dict(zip(headers,r)) for r in rows[:30]]}],"total_filas":len(rows),"total_hojas":1,"contexto":f"ARCHIVO CSV: {nombre}\nFILAS: {len(rows)}\nCOLUMNAS: {len(headers)}\nMÉTRICAS: {numeric[:10]}"}

def extraer_texto(nombre, datos):
    ext=nombre.lower().rsplit('.',1)[-1] if '.' in nombre else ''
    if ext in ('xlsx','xlsm'):
        a=analizar_excel(nombre,datos)
        return a["contexto"], False, a
    if ext=='xls':
        try:
            import xlrd
            book=xlrd.open_workbook(file_contents=datos, on_demand=True)
            hojas=[]; total=0; contexto=[f"ARCHIVO EXCEL ANTIGUO: {nombre}"]
            for sh in book.sheets()[:20]:
                headers=[str(x) for x in sh.row_values(0)] if sh.nrows else []
                rows=[sh.row_values(i) for i in range(1,min(sh.nrows,MAX_FILAS_ANALISIS))]
                sample=[dict(zip(headers,r)) for r in rows[:30]]
                hojas.append({"name":sh.name,"rows":len(rows),"columns":len(headers),"headers":headers,"numeric":[],"categorical":[],"sample":sample})
                total+=len(rows); contexto.append(f"Hoja '{sh.name}': {len(rows)} filas, {len(headers)} columnas.")
            a={"tipo":"excel","nombre":nombre,"hojas":hojas,"total_filas":total,"total_hojas":len(hojas),"contexto":"\n".join(contexto)}
            return a["contexto"], False, a
        except Exception as e:
            raise ValueError("No pude leer el XLS. Instala/activa soporte XLS o conviértelo a XLSX.")
    if ext=='csv':
        a=analizar_csv(nombre,datos)
        return a["contexto"], False, a
    if ext=='pdf':
        from pypdf import PdfReader
        lector=PdfReader(io.BytesIO(datos)); texto="\n\n".join((p.extract_text() or '') for p in lector.pages[:120])
        if not texto.strip(): raise ValueError('El PDF no tiene texto seleccionable. Súbelo como imagen si es un escaneo.')
        analisis={"tipo":"pdf","nombre":nombre,"paginas":len(lector.pages),"caracteres":len(texto)}
    elif ext=='docx':
        import docx
        doc=docx.Document(io.BytesIO(datos)); partes=[p.text for p in doc.paragraphs]
        for tabla in doc.tables:
            for fila in tabla.rows: partes.append(' | '.join(c.text.strip() for c in fila.cells))
        texto='\n'.join(partes)
        analisis={"tipo":"docx","nombre":nombre,"parrafos":len(doc.paragraphs),"tablas":len(doc.tables),"caracteres":len(texto)}
    elif ext=='pptx':
        from pptx import Presentation
        prs=Presentation(io.BytesIO(datos)); partes=[]
        for i,slide in enumerate(prs.slides[:80],1):
            partes.append(f"## Diapositiva {i}")
            for shape in slide.shapes:
                if hasattr(shape,"text") and shape.text.strip(): partes.append(shape.text.strip())
        texto='\n'.join(partes)
        analisis={"tipo":"pptx","nombre":nombre,"diapositivas":len(prs.slides),"caracteres":len(texto)}
    elif ext in EXT_TEXTO:
        for codec in ('utf-8-sig','latin-1'):
            try: texto=datos.decode(codec); break
            except UnicodeDecodeError: continue
        analisis={"tipo":ext,"nombre":nombre,"caracteres":len(texto)}
    else:
        raise ValueError(f'Formato .{ext or "?"} no soportado.')
    return texto[:MAX_TEXTO_ARCHIVO], len(texto)>MAX_TEXTO_ARCHIVO, analisis


def limpiar_mensajes(crudos):
    if not isinstance(crudos,list): raise ValueError('Formato de mensajes inválido.')
    out=[]
    for m in crudos[-MAX_MENSAJES:]:
        rol=m.get('role'); cont=m.get('content')
        if rol not in ('user','assistant'): continue
        if isinstance(cont,str): out.append({'role':rol,'content':cont})
        elif isinstance(cont,list) and rol=='user':
            parts=[]
            for p in cont:
                if p.get('type')=='text' and isinstance(p.get('text'),str): parts.append({'type':'text','text':p['text']})
                elif p.get('type')=='image_url':
                    url=(p.get('image_url') or {}).get('url','')
                    if url.startswith('data:image/'): parts.append({'type':'image_url','image_url':{'url':url}})
            if parts: out.append({'role':rol,'content':parts})
    last=max((i for i,m in enumerate(out) if m['role']=='user'),default=-1)
    for i,m in enumerate(out):
        if isinstance(m['content'],list) and i!=last:
            m['content']=' '.join(p.get('text','') for p in m['content'] if p['type']=='text')+'\n[imagen omitida]'
    def size(m):
        c=m['content']; return len(c) if isinstance(c,str) else sum(len(p.get('text','')) for p in c)
    while len(out)>1 and sum(size(m) for m in out)>MAX_CARACTERES: out.pop(0)
    while out and out[0]['role']!='user': out.pop(0)
    if not out: raise ValueError('No hay mensajes para procesar.')
    return out


def tiene_imagen(ms): return any(isinstance(m['content'],list) and any(p['type']=='image_url' for p in m['content']) for m in ms)

def modelo_no_disponible(e):
    t=str(e).lower(); return any(s in t for s in ('model_not_found','decommission','does not exist','not found','no longer supported'))


def consultar(mensajes):
    candidatos=MODELOS_VISION if tiene_imagen(mensajes) else MODELOS_TEXTO
    ultimo=None
    system={'role':'system','content':SYSTEM_BASE+memory_context()}
    for modelo in candidatos:
        try:
            r=client.chat.completions.create(model=modelo,messages=[system]+mensajes,temperature=.6,max_tokens=MAX_TOKENS_RESPUESTA)
            texto=(r.choices[0].message.content or '').strip(); texto=re.sub(r'<think>.*?</think>','',texto,flags=re.S).strip()
            return texto or 'No obtuve respuesta del modelo.',modelo
        except Exception as e:
            ultimo=e
            if modelo_no_disponible(e): continue
            raise
    raise RuntimeError(f'Ningún modelo configurado está disponible en Groq. Último error: {ultimo}')


@app.errorhandler(413)
def _archivo_muy_grande(e): return jsonify(error=f'El archivo supera el límite de {LIMITE_MB} MB.'),413

# ================= Iconos de la app (pantalla de inicio del iPhone / pestaña) =================
import base64 as _b64
_ICON_180 = _b64.b64decode("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")
_ICON_64 = _b64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAV1klEQVR42oV6W4xk13XdWvvcevSj+jHTM5wZ9sxwmjNkSI5Mk5AU86kEBCjLim04RhIFjm3kw0CQfBgIDPjT3/5xAMN/SoAEiWFYjmIkgRSKhiXZNKkojEVSGs2EIjmcd8/0dE/3VPWrqu7ZKx/n3FtVPaR80R+3q+qee/bZr7XX3uTy00YIxmAAYAFWAJAFpMuCAJGkiUw/E0grAIABAEIhECQKQwigASApGkKRPylGCyI0UV/NFkgACIb0UrP8VaORb1rTOHCF/IkREghofPeEESKUVjSSEgBKipMLuQBIAoDCmD6Uk/mWyDfpDQDgkeWwXsCiICGt4BEA3PF3XnE3P57fkl7jsXqX1y+rBGX9fgCEoInXEEI58YlooMFLRoccMaKMEFDr9sFr/Kv6+H/qZZCgKLmiA4CXn/rTyR1DEXLQkxJGkrgQoyTKayGzDCPZRo94HFgUSESvzwvuGA4xrBTV3/10AcZ2pOhyh5fyEu4CkykfeCaJKkFypW3FvCGVDjkB1jIAStZDG2nVI+JgctEDVhSzIQ2HP92K8qIkGQKD0QxW1Kac7DuZuFc7oJRkYGXXpKv0art5r3Svn9UBk6tNpRLD0hFEn9DDuCF9ohLiroEEjdCkYgqR+ezr5TRpKszeM3JlEDCU0JjxZGFqV45jm4sDACyHHgdJBisdEsoxJfxdhmSV11q1A+QoNGk5nm1ajCU8UkI5svrqpjrXshbYWIIex92AQyBGiukPgHmYCEpJFbUzjF8PyFBkU2EQqHysEFxZBRIIj0k2xZLRBYcVUFKBEAoYLbQw2FelcQ3TnvbRaKMPoJ/V1WoDYDMgpBdNAWC72s4wJi1ZaHptbEkPtTn1d8fTQkEyOytESE74gEUzZysoJTW6K/q8t3fnZtOT862lfO6dhcFMMX4qzZ0SwIEPs2AziwB8du5wJ+eymZUz9bc7lz8GcPXKDWAfEqJnb3EfZbcHNVDlssqEzLKnwpNKCHjpodU4PHsE63cbS4+cPHl6tMby8oFF++dOA7i2fRiALS0CmF9uj3b55CKAXvVvD/i12docnwPwR//6D3zQBgcjD6z18IASipxECcXIELIkXiakoDrBiQCanYXuD340NfTL3bXtrb04P42iwLU7Od8vtPudDpvFzO6+OkeAezx6ztc3bWnx/o39rLfl9szFzSRDff3xdtbVr82WG5duSbsoNBFWPKJoTKS2SgaDaijB2m9YxdasdwsI4HC49/RnW8/+/UbDtrf2AIT7uyhL7K7mNbf2W72eBuVO7x57dwFo7YMDykmSzFzc/GnZNZ8pRzG31sB4WujvjkwIIOE5KJllPCeREFiHgpmVMw8B1/7w+4ef/bmTz72w8OSpT9vDnWF+/bX33up+/cdJyPbMQqezmHQRgMFMoZlFn51T58ihV1/eeXLxj7eLn6+3rslw9CkAZORngo0Cp5ewYgyQSYBcP/vM6b/482/+7C//s4VXn996/a3VC/9ndWZeM4uLx5fGF93oDQDYdnfv5sfz++XU3ML0S78ws3Km9Z0371+/uNHdwWC3WhkBQHN6729eKx89V/z+vwVge0NvF598MAcAUn+3AJkCUcLJBhOQNFD7gEBEod0EsHz+KQDf/Te/zbltAOrOosVeswWAhQHw9gKOPRauv+/rq0tf+Pm9X/h1W1psL7d3Xv+fl177Gpqt9DO1W3UYRYNYPnn6+BJ+5/dXX3kOjWBRHio9lHFkTuPx9IAGPskUKxNKcGDgqxeuLDx56jt/8Id2qM/O0cimHWEqGEBTClnNxlzvWnd99dQ//srmq79hlekvAnryMTYbIhFCgu4E5JElivXVq0uHGrdvHHr9LZ9q2V7/oFfEsYxWh6Nk9JII/zQZqkAkYwTwt+EcTyyoPRetBdJBARITFpO88MZwZ/vxL7y08ewv+vrmKPisnAGDdve1v4/dXe3sabgvj7Sg6dawY1i7AuBi977t9TGMOSWT+Q9AGUcFQ+XNn6KB5ANZBZILASgHl9+/0rvwdVx8G50FKioUTPnFKrDpiOX+9Mzs1PnPH1iyd/Y8YZhdaLaX2lPZfVM6G89l49fO5Y+v7m9jYw0SipCh3sgB+jAroApAxBKhEGLyqyRDLqAA0crIG6/9F+sP4lQHsARdNQaZCCiY4Mcffnh0FOubKZcBmOvuNuZm0d/CfQDrAHYAALvvXQIwfXgBy8dnVs70zp5Pv3/pmdMvAKsXrlx+/8rV21eyGOMuARQU6vCj6AwGj7kOFgwi4QLhaIa4tweARUiPyB0MtOxtSpVcOBjvfH0Ty8c7H164ZT3tDACoQbGFQNDAwN42AAzWtbra+tGl1tLbi8eXZlbOvJtV95mXzj/yAvDm1797/cP3faox7hKB88fGgCfzbmgEWMMPS7gONMICxBS7LFKxxLAPF0P+ELSl2UONow9v7G1z5nBaYFA2jlx6Z/XONTVbmm6jPY1mA80mmw02glpNzEyhEdiw2NJg0Otu3l+7unr342v9W2vTP3h759rmkadOP/Orz/d22+uXfohGM7uHMXDuoQmML9BIGZhQqqXPVdWNjKJEl2Ip53zr0JnTj5188RfXr1/1/g6KwlAsLz+FubkNa2HnXpKB01OH1j5evfY+JIZAsCIgQnUwhBEEI0QHHCwtlgMMtro7Wx/+5P63vjVoHfm5rzw/+OHGnbVraLYgQTC6INW1K4MRQQYB7pKXAORSdJRR0aWoGENhK0fO/L1HP7Py1OdWZ+YvXvxxOewiFJwseg4gPFornYiSJkMDNBVNmClY/qoACHnJcijf9ziwTnP/kYcua3D1P/z7jUu3Xvy9r3Q6D6O/X0UhOSyQVTkS6cHhVQpzyYeQMaEJ+Xxj8fDRI83OwqC3tXrzZvcnF8QBALabuY6r1dm7q86RBIcc5zALNIlBgAWmxE6DWfYZ1jeAhVTo5HoNQATOPH7ryp3VC1cOP3Hi0aNH3/3oppqtuqDRONIg5AkURRAmuFRiOJyfPnp46Uizs7B682bv44887KPRYKtloZlZsMnqf6d3bwZQ50gdRtGaJnaV4HqFUCbq/QlWRgBGFNigv7+wkG4XnjyFS29bSx4O5gEJEbGWpHCAQFGEU8cfS1vvfvCBWgNONWhTiWzLuso1clVd37ihSRrmpU7vT6ZmNBxU+3uA7ZjcPUUVE8hnvt04fv6RiYI4alwA1bRapotCg5FzM4srT31ub+vWT374t7JtdKYYWpLleGUBdTagUaOzbPV628DsaOln2JqBugDEIZBrD0ZXAGi53h+LwjWBR2v75s7Ks+cPP3ECwOX3r6BZJCqgqNVXVTYBZoKFZtEaNE899vjU+c9f/96bGz/+PhZoYdoTb1IEGkGrKdTEnOZDWF7GjRv9rX10Otsba2wW050jh5844Yl7LIRocMGSswHIWEbBsv4r86IVKhrY3T59/MSLv/cVABuXbl3/8P1aOdmJIQdSxpLMrNmeLeZX/tGv9M+dfuc//Tdee4uHm3KLEgtDaKTdw4KRDgKqU7KXrYP2MKgqxlarYpaCmIBNhAXAJgqwGBFCVogV3N2ba8+98Nv/PH156Wt/rf1tNgsAKhqJgZK84nbKMjSKTjG/8sVfWf/Sly9++w1e+zYabbnBmJAwVco1CpgkE/kj4wTVh1avN/7vbOtIBoghxzTIM/+TolyMLMtMpCY578e59tyXf/e3kvFc+LO33rl0Ac1CKpOXW8V7xsR2FlPt1qC18DOfX//Sl2/8x6/yzT9VcwpGGBmY7ZwFK94oE28kxwicVNSnIhPX77Z6vVRhjhPa5BjfGIeQJzEEsUZse7vPPPvZL//ub7128lQynr/6xrdQ7GnYzwqLg8IlS1i6LEMRmuXs9PGVmZef/39f/Xd84796a44SmHc/ouQz9oxCIvYC4EIkDNPVL3dXMX0818rbXQDbs1PADLUjj1KciJ4uIFJBhcMCd/sF2y/+i18//0+eTyX/hT976ztf/ybQPRB2U5PCPJFnzSmUOPncC++9/hr+5muamiNKEAyNXDqbSQZyhDLMIBeGieMSfyq1Xw4BiJExhZc4Vry7rBAjd4aCn37kZ1Zeee69L7383jYAHPsfb3z3T/8zCtACABX5+GChsES7OqwIcW3v2FOPAtBf/i/MTo8svCKOMnuhkcsmyolSqmkYwEa7d/Z86ztvoiwf5GKJHXgJuUoAYAEMDU0CwH4fwNyxlYdfffX2L730XhL59VX85Z9c+ugdTTeTr3MyX47yAJtT6O82OwvXv/cmWz3nHEc4f1Qfk+4RLALkgoEZVMMFI2FqZQ4r3N+NRbai1trdEbMpKbdCDKUJjs3Imcb8sZWHX321d/b87ScX6633blzSfg9TbXiEgQgK44YXiyquDWc4123tDXpbc8C9ifSWPZyBNcWXwamhVguQLQ1T0y91epfq5xNrNHdm49ItbG+jv6+av9mJQuSxhZmnP3vq6cd7Z8/3gPs39v3di/bea3sXvzdsBjCw2YC7rABHmC+HWqKQvOZ0rXPs6rX1c0+dnewduWBIbAWAlGuTJKlCDQUp0aESbLI9O2b0Iys6/MQJ7vSkIcpog31vtnnm1KnHnp5ZOXOjffba+ibeXdXaB+WP/nf/2gdW9H1mjgWSpckMxkyPJoSSMncIBQVWIPjcU2c/+PGH673hI2efu3Ll/zqmyCBVbcMIwWhVwo5AgDKGPdDsQW1FAOLhuRRGtHbbdnd07OHplc+eevpxAFcv31u/fI+9b9h2t/vhdXSvs9jDXEcIVKnSWJhCQKb7qQfgRiECcoVGr3v5OM4tv/yFq++9zeVzjzyCa3c/irvbYoFmo9qgp5BLwAMZDSYwQNFkYgFA+9tv9DqJ6Fo8lROCels/+MZfnH7xi/WLL377DVy/mxhV3t+M/Q0r9326LUyjjAqpvWITSCcTvQk1pWyNwNklKUBQaG/cuHPmxVcOfemLV1dv77J14tGnG52TjWEsh1141HCAGFNMEgR3iHCHIoZRsUTp2B+i0Xj45Mnhu9/vTc02QXYWZk4ca55/YqfRGVy/fP/i+xs/euf+nV10d1sL7Ua/592bZX8zhojQyL3dIqQeMwtmgpaW07YqZJjavi7aQ49LAUA6P4vtk//wl1Zeee7OkFcvj5w5rH7Enc3+7but7t2deA9lhYqLAo0WAGvMl7PtIixMLywsnD2HG6sAdje27nb3sbsaNroAGqdOaX6xtXSEO5vde1e5uSkNlLiPQLKZ02QoUqJMyAVMVjQCLqmHS1IWWBw9l7p3tRjsN4tTy+f/wS8/8U9ffqPXqSnlEcVQ0c41QLDtLoD++t0MH7rXcxg9PNc6caa1dMRn59Jv9m5+HDeu+GAHEooGgoEh0xMWUnerFgCFsTYhIwWZ1QKABjPakXPJsMSQ6blGWyQxrbmjp596ZuWV52rC+Ub77LgM7N3d3lhr9Xr9rf26RXCAtAKwubq+GIeHjj984bv/Pe5seqvgeP3BwCLUzCSsUZWHxqJCDXLQlCDwpBJoR87l0M5AxVoP1pxyazBS04c4P9WcXz62uIDl44fiRLS5Fyaww+bqOoDlFz5398+/qd4WO7kIXF6Yf/Grv/NHv/qvuHYHgGMIEmY5HJPM9bQhFPmAaxmSD6SGIEd94SRAkYYlRFBRDKi7fIM9FkNaQ/0NrWGwevMaAOBqA2bmNRU/KMESUfShyhKAzS72fvNfLi+8efnmT9BdS62QwcLUE5duHZxjGG+TpjqQiVFmDjiUSgLGBiARFEdtF8ohFO5uBshGMgDBojOoLFEAKkkDq/ZrOQkH3REAM4R2aNFdarU6H17Y27rV/fCyLR3PtP7cUQAoOUZAqcqGKUl6BcshOGGykOId6BoCBYhMjmRagJSqEpZwpj7r2DQKA6CSEjSAR8ldpVAKJdxBV+q0KJtvTGCp3299cBUAWlS8D8D7m8PuWgJzHvtChEfIISFGKMIdUVCkRxwch0kR01HmsYaUuADII+QH0ydpaTCFTOwWcjZnJMox3soTsMvEYzAEkC6ZlxE3bgx6W+hLpXt/E719AO++cxV7O7lrNAm6qUoDcshTgzS3x+uoL6GEPDJG+mhwwezB/mvqp7tDnnAcXLmMMhHOwPGGFenw2imGPFASDPposTF39Np770OOWNZzRPVxJGCovFWHD5lmaOJAUspuhKCYJ3rkSGLEGGxq3pWgsjLyH3WN6dLI3jn2GzOrMCiUEyURFQnDwtRsHOxvra5hUHJueu7Q4U2JUXHnNvrDFFJSW5SjoQvLjVISDISQOGQXoKrLzZwRKs9nCgKGPMWQJE3HMBrY8NIUSSU9VPM1cXykRC4oJozN4QDAxvrdqeNHzzz7mU5nrru5FTa6cw6rm6RZseNTLJ6sCBJqJlmeRzJiWU9ZUaVKrzNJsOlFjLN8Up4xSG6edQEo09b55MhsnxKMktEMKODDlROPzz600p5qTjWL26t3dtbXEArt3S/oxzuHNre7CJaWyFvI2k6kmEOJpp7omyQVi0QUFfMEnBPuRVVSE5V3Vy6a0jpqhjBDqcn5t4maDQiIzc7C3tat1Zs3t7f2gGCdYwB8dr+3cRs4ZkXwMibnA+Du1TBgKSMABqEMYqXfQMlJB40xZsuOJSwAkSSLpTOemER3C3QxB+nxqaAUsyu3/9S5PDNY0E6brd4nM579Dqen1CpFglU1a42M4SgYwdz+QTAkRJ3onAR+5ECR+0MW6JHF0pmREsYb/Kl3RiNNE+Nu1UANx+rpooAFwqQmALZMfQfA/X7dEgagINLFMNqrYr4BYGAKTplFYEIZFT06RuMyHCzqPeqA5eXIYJb6Sj7GBaiihFkU1WCpMQSXjarkIACaaTIybR1GOJSiV41EGPJLDUjFXq5jHLCxAOU1kSMScKggXRY+vdGd/LiqJw3wSu7qTNJYWhqaDSMcVQRPJpG4yqBsIZkSg5k5HEbAwUBKOsC0e6WQPK1XgaXsmCLT7uHDiloMPDg9SIOZCK/+NcjqPF/ZT8oTGZJQMKGMeeitPuaxIYwMBBnyQVQLsgp8Gd4lxjLl7FhtaQxlCA4fjmgVFw+M9VlNcDMRQFmJOWWpBKtOuDttiFSXKpJBsk8fPY2IWRUa69Wmt3NshJMxZms5MB4qT8EcckJFdoCczg2ATTZ66aXBNDajaLQsg9Fo8oRknLDqBrA8J8sQctANYz0sGeHy1JsjKDnSWk5jqrkP9J3y2MDB6Fd41IOmnyM0s/KT9+VcECxjQJo7hJgJuZwNVQ3yVskycbiWxpFyBZPaGcbqdD2yMuKE4fLcSJ1d81Ma5wiTSMXYLGoiWODuGV+MnUGmt2xClVkVLlis0M3YJGeeQSpJmizWlUzajU06rkeNNxrHAMHIscPYnDaD4OQnjgKmoeIHiKo80aKJKWirqj/CHxx5IdxARjicEFUanfTUIpnAv/Kx0XMwF1xV80Hj09ZZswmf/n8YTlBqdPudKgAAAABJRU5ErkJggg==")

@app.route('/apple-touch-icon.png')
@app.route('/apple-touch-icon-precomposed.png')
@app.route('/apple-touch-icon-180x180.png')
def icono_apple():
    r=Response(_ICON_180,mimetype='image/png'); r.headers['Cache-Control']='public, max-age=86400'; return r

@app.route('/favicon.ico')
@app.route('/icon.png')
def icono_pestana():
    r=Response(_ICON_64,mimetype='image/png'); r.headers['Cache-Control']='public, max-age=86400'; return r

@app.route('/')
def index(): return render_template('index.html',requiere_clave=bool(CLAVE_ACCESO),storage=db_kind())
@app.route('/salud')
def salud(): return jsonify(ok=True,storage=db_kind())

@app.route('/diagnostico')
def diagnostico():
    db_ok=False
    db_error=''
    try:
        if USE_POSTGRES:
            with pg_conn() as c: c.execute('SELECT 1')
        else:
            with sqlite_conn() as c: c.execute('SELECT 1')
        db_ok=True
    except Exception as e:
        db_error=str(e)[:300]
    memoria_ok=False
    memoria_error=''
    try:
        list_memories()
        memoria_ok=True
    except Exception as e:
        memoria_error=str(e)[:300]
    return jsonify({
        'ok': bool(client) and db_ok and memoria_ok,
        'groq_configurada': bool(GROQ_API_KEY),
        'base_datos': db_kind(),
        'base_datos_ok': db_ok,
        'memoria_lectura_ok': memoria_ok,
        'database_url_configurada': bool(DATABASE_URL),
        'clave_tampy_configurada': bool(CLAVE_ACCESO),
        'modelo_texto': MODELOS_TEXTO[0] if MODELOS_TEXTO else None,
        'modelo_vision': MODELOS_VISION[0] if MODELOS_VISION else None,
        'detalle_bd': db_error,
        'detalle_memoria': memoria_error
    })

@app.route('/api/conversations',methods=['GET'])
def api_conversations():
    if not autorizado(): return jsonify(error='Clave incorrecta.'),401
    return jsonify(conversaciones=list_conversations())

@app.route('/api/conversations/<cid>',methods=['GET','DELETE'])
def api_conversation(cid):
    if not autorizado(): return jsonify(error='Clave incorrecta.'),401
    if request.method=='DELETE': delete_conversation(cid); return jsonify(ok=True)
    d=get_conversation(cid)
    return jsonify(d) if d else (jsonify(error='Conversación no encontrada.'),404)

@app.route('/api/conversations',methods=['POST'])
def api_save_conversation():
    if not autorizado(): return jsonify(error='Clave incorrecta.'),401
    d=request.get_json(silent=True) or {}; msgs=d.get('mensajes',[]); cid=d.get('id') or str(uuid.uuid4()); title=(d.get('title') or 'Nueva conversación')[:120]
    save_conversation(cid,title,msgs); return jsonify(id=cid)

@app.route('/api/memories',methods=['GET','POST'])
def api_memories():
    if not autorizado(): return jsonify(error='Clave incorrecta.'),401
    if request.method=='GET': return jsonify(memories=list_memories())
    d=request.get_json(silent=True) or {}; content=str(d.get('content','')).strip(); category=str(d.get('category','general'))[:50]
    if not content or len(content)>1000: return jsonify(error='Memoria inválida.'),400
    return jsonify(id=add_memory(content,category))

@app.route('/api/memories/<mid>',methods=['DELETE'])
def api_memory(mid):
    if not autorizado(): return jsonify(error='Clave incorrecta.'),401
    delete_memory(mid); return jsonify(ok=True)

@app.route('/api/archivo',methods=['POST'])
def api_archivo():
    if not autorizado(): return jsonify(error='Clave incorrecta.'),401
    f=request.files.get('archivo')
    if not f or not f.filename: return jsonify(error='No llegó ningún archivo.'),400
    raw=f.read()
    if len(raw) > app.config["MAX_CONTENT_LENGTH"]: return jsonify(error=f'El archivo supera el límite de {LIMITE_MB} MB.'),413
    try:
        texto,truncado,analisis=extraer_texto(f.filename,raw)
    except Exception as e:
        return jsonify(error=str(e)),400
    # Se conserva una copia local para poder crear artefactos durante esta sesión.
    safe=re.sub(r'[^A-Za-z0-9._-]','_',f.filename)[:100]
    file_id=uuid.uuid4().hex
    path=UPLOAD_DIR / f"{file_id}_{safe}"
    path.write_bytes(raw)
    return jsonify(id=file_id,nombre=f.filename,texto=texto,truncado=truncado,analisis=analisis)

def _leer_analisis(fa):
    """Lee el análisis JSON que ya calculó el navegador al subir el archivo (evita volver a procesar un Excel pesado)."""
    a=json.loads(fa.read().decode('utf-8'))
    if not isinstance(a,dict) or not isinstance(a.get('hojas'),list):
        raise ValueError('Análisis inválido. Vuelve a subir el archivo.')
    a['nombre']=str(a.get('nombre') or 'archivo.xlsx')[:150]
    return a

def _dashboard_html(analisis, titulo=None):
    import html as _html, json as _json
    titulo=titulo or f"Dashboard — {analisis.get('nombre','Archivo')}"
    payload=_json.dumps(analisis,ensure_ascii=False).replace("</","<\\/")
    template=r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f6f8fc;--card:#fff;--text:#172033;--muted:#667085;--line:#e5e7eb;--accent:#315efb}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif}
header{background:linear-gradient(135deg,#111827,#315efb);color:white;padding:28px 5vw}header h1{margin:0 0 6px;font-size:clamp(24px,4vw,38px)}header p{margin:0;opacity:.82}
main{max-width:1400px;margin:auto;padding:24px 5vw}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;box-shadow:0 5px 18px #1018280d}.kpi b{display:block;font-size:28px;margin-top:5px}.muted{color:var(--muted);font-size:13px}
section{margin:20px 0}h2{font-size:20px;margin:0 0 12px}table{width:100%;border-collapse:collapse;font-size:13px;background:var(--card)}th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:left}th{background:#f1f4ff;position:sticky;top:0}.tablewrap{overflow:auto;max-height:520px;border:1px solid var(--line);border-radius:14px}
.bar{height:12px;border-radius:10px;background:#dbe3ff;overflow:hidden;margin:7px 0 14px}.bar i{display:block;height:100%;background:var(--accent)}.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}input{padding:10px 12px;border:1px solid var(--line);border-radius:10px;min-width:240px}
footer{text-align:center;color:var(--muted);padding:25px}
</style></head><body>
<header><h1>__TITLE__</h1><p>Generado automáticamente por TAMPY PRO • análisis local con Python</p></header>
<main id="app"></main><footer>TAMPY PRO • Dashboard autónomo</footer>
<script>
const DATA=__DATA__;
const root=document.getElementById('app');
function esc(v){return String(v??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
function fmt(v){return Number(v).toLocaleString('es-EC',{maximumFractionDigits:2})}
root.innerHTML='<div class="grid">'+
'<div class="card kpi"><span class="muted">Filas analizadas</span><b>'+fmt(DATA.total_filas||0)+'</b></div>'+
'<div class="card kpi"><span class="muted">Hojas</span><b>'+fmt(DATA.total_hojas||1)+'</b></div>'+
'<div class="card kpi"><span class="muted">Archivo</span><b style="font-size:18px">'+esc(DATA.nombre)+'</b></div>'+
'<div class="card kpi"><span class="muted">Motor</span><b style="font-size:18px">Python</b></div></div>';
(DATA.hojas||[]).forEach(function(h,hi){
  var cards=(h.numeric||[]).slice(0,8).map(function(n){return '<div class="card"><span class="muted">'+esc(n.column)+'</span><b>'+fmt(n.sum)+'</b><div class="muted">Promedio '+fmt(n.avg)+' • min '+fmt(n.min)+' • max '+fmt(n.max)+'</div></div>'}).join('');
  var cats=(h.categorical||[]).slice(0,6).map(function(c){var max=(c.top&&c.top[0]?c.top[0].count:1);return '<div class="card"><b>'+esc(c.column)+'</b><div class="muted">'+c.unique+' valores únicos</div>'+c.top.map(function(x){return '<div style="margin-top:9px">'+esc(x.value)+' <span class="muted">('+x.count+')</span><div class="bar"><i style="width:'+Math.min(100,x.count/max*100)+'%"></i></div></div>'}).join('')+'</div>'}).join('');
  var rows=(h.sample||[]).map(function(r){return '<tr>'+h.headers.map(function(k){return '<td>'+esc(r[k])+'</td>'}).join('')+'</tr>'}).join('');
  root.insertAdjacentHTML('beforeend','<section><h2>📊 '+esc(h.name)+'</h2><div class="grid">'+cards+'</div>'+cats+'<div class="card"><h2>Vista de datos</h2><div class="toolbar"><input placeholder="Filtrar esta hoja..." oninput="filtrar(this,'+hi+')"></div><div class="tablewrap"><table id="tabla'+hi+'"><thead><tr>'+h.headers.map(function(x){return '<th>'+esc(x)+'</th>'}).join('')+'</tr></thead><tbody>'+rows+'</tbody></table></div></div></section>');
});
function filtrar(inp,idx){var q=inp.value.toLowerCase();document.querySelectorAll('#tabla'+idx+' tbody tr').forEach(function(tr){tr.style.display=tr.innerText.toLowerCase().includes(q)?'':'none'})}
</script></body></html>"""
    return template.replace("__TITLE__", _html.escape(titulo)).replace("__DATA__", payload)

def _artifact_path(file_id):
    matches=list(UPLOAD_DIR.glob(file_id+"_*.xlsx"))+list(UPLOAD_DIR.glob(file_id+"_*.xlsm"))+list(UPLOAD_DIR.glob(file_id+"_*.csv"))
    return matches[0] if matches else None


def _infer_roles(headers):
    roles = {}
    patterns = {
        'producto': r'producto|item|articulo|artículo|sku|codigo|código|descripcion|descripción',
        'proveedor': r'proveedor|supplier|marca',
        'sucursal': r'sucursal|tienda|local|agencia|branch',
        'categoria': r'categor[ií]a|familia|departamento|linea|línea|grupo',
        'fecha': r'fecha|date|periodo|mes|año|ano',
        'ventas': r'venta|ventas|factur|ingreso|revenue',
        'compras': r'compra|compras|pedido|purchase',
        'costo': r'costo|cost|precio.?compra|unit.?cost',
        'precio': r'precio|pvp|price|venta.?unit',
        'stock': r'stock|existencia|inventario|disponible|saldo',
        'cantidad': r'cantidad|unidades|qty|units',
        'margen': r'margen|margin|utilidad|ganancia|profit',
    }
    for h in headers:
        hs = str(h).lower().strip()
        for role, pat in patterns.items():
            if re.search(pat, hs, re.I):
                roles.setdefault(role, []).append(h)
    return roles


def _multi_dashboard_html(items, titulo='TAMPY • Dashboard multiarchivo'):
    import html as _html, json as _json
    roles = {}
    for a in items:
        headers = sum([h.get('headers', []) for h in a.get('hojas', [])], [])
        for k, v in _infer_roles(headers).items():
            roles.setdefault(k, []).extend(v)
    payload = {'archivos': items, 'roles': {k:list(dict.fromkeys(v)) for k,v in roles.items()}, 'total_archivos':len(items)}
    data = _json.dumps(payload, ensure_ascii=False).replace('</', '<\\/')
    title = _html.escape(titulo)
    return f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{margin:0;background:#f6f8fc;color:#172033;font-family:Inter,system-ui,sans-serif}}header{{background:linear-gradient(135deg,#111827,#315efb);color:#fff;padding:28px 5vw}}main{{max-width:1500px;margin:auto;padding:24px 5vw}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px}}.card{{background:#fff;border:1px solid #e5e7eb;border-radius:16px;padding:16px;margin-bottom:16px}}.muted{{color:#667085;font-size:13px}}b.big{{display:block;font-size:28px;margin-top:5px}}.pill{{display:inline-block;background:#eef2ff;padding:6px 10px;border-radius:999px;margin:3px;font-size:13px}}table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{padding:8px;border-bottom:1px solid #e5e7eb;text-align:left;white-space:nowrap}}th{{background:#f1f4ff;position:sticky;top:0}}.wrap{{overflow:auto;max-height:480px;border:1px solid #e5e7eb;border-radius:12px}}input{{padding:10px;border:1px solid #ddd;border-radius:10px;width:min(420px,90%)}}footer{{text-align:center;color:#667085;padding:25px}}</style></head><body><header><h1>{title}</h1><p>Generado por TAMPY PRO 4.0 • Python + análisis automático</p></header><main id="app"></main><footer>TAMPY PRO 4.0</footer>
<script>const D={data};const A=document.getElementById('app');function e(v){{return String(v??'').replace(/[&<>"']/g,m=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[m]))}}function n(v){{return Number(v).toLocaleString('es-EC',{{maximumFractionDigits:2}})}}
A.innerHTML='<div class="grid"><div class="card"><span class="muted">Archivos</span><b class="big">'+D.total_archivos+'</b></div><div class="card"><span class="muted">Hojas</span><b class="big">'+D.archivos.reduce((s,a)=>s+(a.total_hojas||0),0)+'</b></div><div class="card"><span class="muted">Filas</span><b class="big">'+n(D.archivos.reduce((s,a)=>s+(a.total_filas||0),0))+'</b></div><div class="card"><span class="muted">Campos detectados</span><div>'+Object.keys(D.roles).map(k=>'<span class="pill">'+e(k)+'</span>').join('')+'</div></div></div>';
D.archivos.forEach((a,ai)=>{{A.insertAdjacentHTML('beforeend','<section><h2>📊 '+e(a.nombre)+'</h2>'+(a.hojas||[]).map((h,hi)=>'<div class="card"><h3>'+e(h.name)+'</h3><div class="grid">'+(h.numeric||[]).slice(0,8).map(x=>'<div><span class="muted">'+e(x.column)+'</span><b class="big">'+n(x.sum)+'</b><span class="muted">Promedio '+n(x.avg)+' • min '+n(x.min)+' • max '+n(x.max)+'</span></div>').join('')+'</div><p><input placeholder="Filtrar esta hoja..." oninput="f(this,'+ai+'_'+hi+')"></p><div class="wrap"><table id="t'+ai+'_'+hi+'"><thead><tr>'+h.headers.map(x=>'<th>'+e(x)+'</th>').join('')+'</tr></thead><tbody>'+(h.sample||[]).map(r=>'<tr>'+h.headers.map(k=>'<td>'+e(r[k])+'</td>').join('')+'</tr>').join('')+'</tbody></table></div></div>').join('')+'</section>')}});function f(i,k){{const q=i.value.toLowerCase();document.querySelectorAll('#t'+k+' tbody tr').forEach(r=>r.style.display=r.innerText.toLowerCase().includes(q)?'':'none')}};</script></body></html>'''


def _landing_fallback(nombre='Mi negocio'):
    return f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{nombre}</title><style>body{{margin:0;font-family:Inter,system-ui;background:#0f172a;color:white}}main{{min-height:100vh;display:grid;place-items:center;text-align:center;padding:40px}}.box{{max-width:800px}}h1{{font-size:clamp(44px,8vw,82px)}}p{{font-size:20px;color:#cbd5e1;line-height:1.6}}a{{display:inline-block;margin-top:20px;padding:14px 22px;border-radius:999px;background:white;color:#111827;text-decoration:none;font-weight:700}}</style></head><body><main><div class="box"><h1>{nombre}</h1><p>Landing page profesional creada con TAMPY.</p><a href="#contacto">Contáctanos</a></div></main></body></html>'''




def _safe_sheet_name(name):
    return str(name)[:31] or 'Hoja1'


def _norm_text(v):
    return re.sub(r'\s+', ' ', str(v or '').strip().lower())


def _coerce_number(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    if v is None:
        return None
    t=str(v).strip().replace('$','').replace(' ','')
    if not t:
        return None
    # Ecuador/Latin and common spreadsheet number formats.
    if ',' in t and '.' in t:
        if t.rfind(',') > t.rfind('.'):
            t=t.replace('.','').replace(',','.')
        else:
            t=t.replace(',','')
    elif ',' in t:
        parts=t.split(',')
        if len(parts[-1]) in (1,2,3): t='.'.join(parts)
        else: t=''.join(parts)
    try:
        return float(t)
    except Exception:
        return None


def _cmp(a, op, b):
    op=str(op or 'equals').lower()
    if op in ('contains','not_contains'):
        aa=_norm_text(a); bb=_norm_text(b); hit=_norm_text(b) in aa
        return hit if op=='contains' else not hit
    na,nb=_coerce_number(a),_coerce_number(b)
    if na is not None and nb is not None: a,b=na,nb
    else: a,b=_norm_text(a),_norm_text(b)
    if op in ('equals','eq','='): return a==b
    if op in ('not_equals','ne','!='): return a!=b
    if op in ('gt','>'): return a>b
    if op in ('gte','>='): return a>=b
    if op in ('lt','<'): return a<b
    if op in ('lte','<='): return a<=b
    if op in ('is_blank','blank'): return a in ('',None)
    if op in ('not_blank','notblank'): return a not in ('',None)
    return False


def _header_map(ws):
    headers=[]
    for c in range(1, ws.max_column+1):
        v=ws.cell(1,c).value
        h=str(v).strip() if v is not None else ''
        if not h: h=f'Columna_{c}'
        base=h; n=2
        while h in headers:
            h=f'{base}_{n}'; n+=1
        headers.append(h)
    return headers


def _resolve_col(headers, requested):
    if requested is None: return None
    r=_norm_text(requested)
    for h in headers:
        if _norm_text(h)==r: return h
    for h in headers:
        if r and (r in _norm_text(h) or _norm_text(h) in r): return h
    return None


def _plan_with_ai(prompt, workbook_info):
    if not client:
        return None
    system='''Eres un planificador de transformaciones de archivos. Devuelve SOLO JSON válido, sin Markdown. Nunca inventes columnas. Debes escoger únicamente operaciones seguras de esta lista: remove_empty_rows, remove_duplicates, replace_values, fill_blank, delete_rows_where, rename_column, add_column_constant, trim_text, sort_by, convert_number. Cada operación debe indicar sheet cuando aplique. Para delete_rows_where usa column, operator y value. Para replace_values usa column, from y to. Para fill_blank usa column y value. Para rename_column usa from y to. Para add_column_constant usa column y value. Para trim_text usa column. Para sort_by usa column y direction asc/desc. Para convert_number usa column. Si la petición es ambigua o no se puede ejecutar con seguridad, devuelve operations=[] y una explicación breve en note. Formato: {"operations":[...],"note":"..."}.'''
    user='PETICIÓN DEL USUARIO:\n'+prompt+'\n\nESTRUCTURA DEL ARCHIVO:\n'+json.dumps(workbook_info,ensure_ascii=False)[:30000]
    try:
        r=client.chat.completions.create(model=MODELOS_TEXTO[0],messages=[{'role':'system','content':system},{'role':'user','content':user}],temperature=0,max_tokens=2500)
        raw=(r.choices[0].message.content or '').strip()
        raw=re.sub(r'^```(?:json)?\s*|\s*```$','',raw,flags=re.I|re.S).strip()
        return json.loads(raw)
    except Exception:
        log.exception('No se pudo generar plan de transformación con IA')
        return None


def _heuristic_plan(prompt, workbook_info):
    p=_norm_text(prompt); ops=[]
    sheets=workbook_info.get('sheets',[])
    if re.search(r'duplicad|repetid',p): ops.append({'op':'remove_duplicates'})
    if re.search(r'fila[s]? vac[ií]a|filas vacias|elimina.*vac',p): ops.append({'op':'remove_empty_rows'})
    # Common request: remove negative values from numeric data. Only target an explicitly named column if possible.
    if re.search(r'elimina|quitar|corrige|corregir|remueve',p) and re.search(r'negativ',p):
        cols=[]
        # Prefer columns explicitly named in the user's request.
        for sh in sheets:
            for h in sh.get('headers',[]):
                if _norm_text(h) in p or re.search(r'\b'+re.escape(_norm_text(h))+r'\b',p):
                    cols.append(h)
        if not cols:
            for sh in sheets:
                for h in sh.get('headers',[]):
                    if re.search(r'valor|venta|costo|precio|cantidad|stock|existencia|total',_norm_text(h)):
                        cols.append(h)
        for c in list(dict.fromkeys(cols))[:12]: ops.append({'op':'delete_rows_where','column':c,'operator':'lt','value':0})
    return {'operations':ops,'note':'Plan generado localmente porque el planificador IA no estuvo disponible.'}


def _build_workbook_info(wb):
    info={'sheets':[]}
    for ws in wb.worksheets:
        headers=_header_map(ws)
        samples=[]
        for row in ws.iter_rows(min_row=2,max_row=min(ws.max_row,6),values_only=True):
            samples.append({headers[i]:row[i] for i in range(min(len(headers),len(row)))})
        info['sheets'].append({'name':ws.title,'max_rows':ws.max_row,'max_columns':ws.max_column,'headers':headers,'samples':samples})
    return info


def _apply_ops_xlsx(wb, operations):
    report=[]
    for op in operations:
        typ=str(op.get('op','')).strip()
        target_sheets=[wb[op['sheet']]] if op.get('sheet') in wb.sheetnames else list(wb.worksheets)
        changed=0
        for ws in target_sheets:
            headers=_header_map(ws)
            # Normalize first row headers only when needed; don't overwrite original names.
            col_name=op.get('column')
            col=_resolve_col(headers,col_name)
            if typ=='remove_empty_rows':
                for r in range(ws.max_row,1,-1):
                    if all(ws.cell(r,c).value in (None,'') for c in range(1,ws.max_column+1)):
                        ws.delete_rows(r,1); changed+=1
            elif typ=='remove_duplicates':
                wanted=op.get('columns') or headers
                idx=[]
                for h in wanted:
                    rr=_resolve_col(headers,h)
                    if rr: idx.append(headers.index(rr)+1)
                if not idx: idx=list(range(1,ws.max_column+1))
                seen=set()
                for r in range(ws.max_row,1,-1):
                    key=tuple(ws.cell(r,c).value for c in idx)
                    if key in seen:
                        ws.delete_rows(r,1); changed+=1
                    else: seen.add(key)
            elif typ=='replace_values' and col:
                ci=headers.index(col)+1; old=op.get('from'); new=op.get('to')
                for r in range(2,ws.max_row+1):
                    if _norm_text(ws.cell(r,ci).value)==_norm_text(old) or ws.cell(r,ci).value==old:
                        ws.cell(r,ci).value=new; changed+=1
            elif typ=='fill_blank' and col:
                ci=headers.index(col)+1; val=op.get('value','')
                for r in range(2,ws.max_row+1):
                    if ws.cell(r,ci).value in (None,''):
                        ws.cell(r,ci).value=val; changed+=1
            elif typ=='delete_rows_where' and col:
                ci=headers.index(col)+1
                for r in range(ws.max_row,1,-1):
                    if _cmp(ws.cell(r,ci).value,op.get('operator','equals'),op.get('value')):
                        ws.delete_rows(r,1); changed+=1
            elif typ=='rename_column' and op.get('from') is not None:
                old=_resolve_col(headers,op.get('from'))
                if old:
                    ws.cell(1,headers.index(old)+1).value=str(op.get('to') or old); changed+=1
            elif typ=='add_column_constant':
                name=str(op.get('column') or 'Nueva_columna'); ci=ws.max_column+1
                ws.cell(1,ci).value=name
                for r in range(2,ws.max_row+1): ws.cell(r,ci).value=op.get('value',''); changed+=1
            elif typ=='trim_text' and col:
                ci=headers.index(col)+1
                for r in range(2,ws.max_row+1):
                    v=ws.cell(r,ci).value
                    if isinstance(v,str):
                        nv=re.sub(r'\s+',' ',v).strip()
                        if nv!=v: ws.cell(r,ci).value=nv; changed+=1
            elif typ=='sort_by' and col:
                ci=headers.index(col)+1; rows=list(ws.iter_rows(min_row=2,values_only=True))
                rev=str(op.get('direction','asc')).lower()=='desc'
                rows.sort(key=lambda x: (x[ci-1] is None, str(x[ci-1]).lower()), reverse=rev)
                for r_idx,row in enumerate(rows,2):
                    for c_idx,val in enumerate(row,1): ws.cell(r_idx,c_idx).value=val
                changed=len(rows)
            elif typ=='convert_number' and col:
                ci=headers.index(col)+1
                for r in range(2,ws.max_row+1):
                    v=ws.cell(r,ci).value; n=_coerce_number(v)
                    if n is not None and v!=n: ws.cell(r,ci).value=n; changed+=1
        report.append({'op':typ,'changed':changed,'sheet':op.get('sheet') or 'todas'})
    return report


def _process_csv(raw, prompt, filename):
    text=raw.decode('utf-8-sig',errors='replace')
    dialect=csv.Sniffer().sniff(text[:10000]) if text.strip() else csv.excel
    rows=list(csv.reader(io.StringIO(text),dialect))
    if not rows: raise ValueError('CSV vacío.')
    headers=rows[0]; data=rows[1:]
    info={'sheets':[{'name':'CSV','max_rows':len(rows),'max_columns':len(headers),'headers':headers,'samples':[dict(zip(headers,r)) for r in data[:5]]}]}
    plan=_plan_with_ai(prompt,info) or _heuristic_plan(prompt,info)
    ops=plan.get('operations',[]) if isinstance(plan,dict) else []
    changed=0
    for op in ops:
        typ=op.get('op'); col=_resolve_col(headers,op.get('column')); ci=headers.index(col) if col in headers else None
        if typ=='remove_empty_rows':
            old=len(data); data=[r for r in data if any(str(x).strip() for x in r)]; changed+=old-len(data)
        elif typ=='remove_duplicates':
            wanted=op.get('columns') or headers; idx=[headers.index(_resolve_col(headers,x)) for x in wanted if _resolve_col(headers,x)] or list(range(len(headers))); seen=set(); out=[]
            for r in data:
                key=tuple(r[i] if i<len(r) else '' for i in idx)
                if key in seen: changed+=1
                else: seen.add(key); out.append(r)
            data=out
        elif typ=='replace_values' and ci is not None:
            for r in data:
                if ci<len(r) and (_norm_text(r[ci])==_norm_text(op.get('from')) or r[ci]==op.get('from')): r[ci]=str(op.get('to','')); changed+=1
        elif typ=='fill_blank' and ci is not None:
            for r in data:
                while len(r)<len(headers): r.append('')
                if not str(r[ci]).strip(): r[ci]=str(op.get('value','')); changed+=1
        elif typ=='delete_rows_where' and ci is not None:
            old=len(data); data=[r for r in data if not _cmp(r[ci] if ci<len(r) else '',op.get('operator','equals'),op.get('value'))]; changed+=old-len(data)
        elif typ=='rename_column' and op.get('from') is not None:
            old=_resolve_col(headers,op.get('from'))
            if old: headers[headers.index(old)]=str(op.get('to') or old); changed+=1
        elif typ=='add_column_constant':
            headers.append(str(op.get('column') or 'Nueva_columna')); val=str(op.get('value',''))
            for r in data: r.append(val)
            changed+=len(data)
        elif typ=='trim_text' and ci is not None:
            for r in data:
                if ci<len(r):
                    nv=re.sub(r'\s+',' ',str(r[ci])).strip()
                    if nv!=r[ci]: r[ci]=nv; changed+=1
        elif typ=='convert_number' and ci is not None:
            for r in data:
                if ci<len(r):
                    n=_coerce_number(r[ci])
                    if n is not None and str(n)!=str(r[ci]): r[ci]=n; changed+=1
    out=io.StringIO(); csv.writer(out).writerows([headers]+data)
    return out.getvalue().encode('utf-8-sig'),plan,changed


@app.route('/api/archivo/procesar',methods=['POST'])
def api_procesar_archivo():
    if not autorizado(): return jsonify(error='Clave incorrecta.'),401
    try:
        prompt=(request.form.get('instruccion') or request.form.get('prompt') or '').strip()
        if not prompt: return jsonify(error='Escribe qué quieres corregir o modificar en el archivo.'),400
        f=request.files.get('archivo')
        file_id=(request.form.get('file_id') or '').strip()
        if f and f.filename:
            raw=f.read(); nombre=f.filename
        elif file_id:
            path=_artifact_path(file_id)
            if not path: return jsonify(error='El archivo ya no está disponible. Vuelve a subirlo.'),404
            raw=path.read_bytes(); nombre=path.name.split('_',1)[1] if '_' in path.name else path.name
        else: return jsonify(error='Adjunta un archivo.'),400
        ext=Path(nombre).suffix.lower()
        base=re.sub(r'[^A-Za-z0-9_-]','_',Path(nombre).stem)[:70] or 'TAMPY_archivo'
        if ext in ('.xlsx','.xlsm','.xls'):
            from openpyxl import load_workbook
            if ext=='.xls':
                # xlrd can read XLS, but not safely preserve/write all formatting; return a clean XLSX.
                import xlrd
                book=xlrd.open_workbook(file_contents=raw, formatting_info=False)
                from openpyxl import Workbook
                wb=Workbook(); wb.remove(wb.active)
                for sh in book.sheets():
                    ws=wb.create_sheet(_safe_sheet_name(sh.name))
                    for r in range(sh.nrows):
                        for c in range(sh.ncols): ws.cell(r+1,c+1).value=sh.cell_value(r,c)
            else:
                wb=load_workbook(io.BytesIO(raw), data_only=False, keep_vba=(ext=='.xlsm'))
            info=_build_workbook_info(wb)
            plan=_plan_with_ai(prompt,info) or _heuristic_plan(prompt,info)
            ops=plan.get('operations',[]) if isinstance(plan,dict) else []
            if not ops:
                return jsonify(error=plan.get('note') or 'No encontré una modificación segura que aplicar.', plan=plan),400
            report=_apply_ops_xlsx(wb,ops)
            out=io.BytesIO(); wb.save(out); out.seek(0)
            out_ext='.xlsm' if ext=='.xlsm' else '.xlsx'
            mime='application/vnd.ms-excel.sheet.macroEnabled.12' if out_ext=='.xlsm' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            return send_file(out,as_attachment=True,download_name=base+'_corregido'+out_ext,mimetype=mime)
        elif ext=='.csv':
            out,plan,changed=_process_csv(raw,prompt,nombre)
            if not plan.get('operations'): return jsonify(error=plan.get('note') or 'No encontré una modificación segura que aplicar.',plan=plan),400
            return send_file(io.BytesIO(out),as_attachment=True,download_name=base+'_corregido.csv',mimetype='text/csv; charset=utf-8')
        elif ext in ('.txt','.md','.json'):
            text=raw.decode('utf-8-sig',errors='replace')
            # Safe text operations are intentionally conservative.
            if re.search(r'quitar espacios|limpiar espacios|espacios dobles',_norm_text(prompt)):
                text='\n'.join(re.sub(r'[ \t]+',' ',line).strip() for line in text.splitlines())
            elif re.search(r'convertir.*may[uú]scula|todo.*may[uú]scula',_norm_text(prompt)):
                text=text.upper()
            elif re.search(r'convertir.*min[uú]scula|todo.*min[uú]scula',_norm_text(prompt)):
                text=text.lower()
            else:
                return jsonify(error='Para TXT/MD/JSON todavía necesito una instrucción de limpieza compatible (espacios o mayúsculas/minúsculas).'),400
            out_ext=ext; mime='application/json' if ext=='.json' else 'text/plain; charset=utf-8'
            return send_file(io.BytesIO(text.encode('utf-8')),as_attachment=True,download_name=base+'_corregido'+out_ext,mimetype=mime)
        else:
            return jsonify(error='En esta versión el procesamiento directo soporta XLSX, XLSM, XLS, CSV, TXT, MD y JSON.'),400
    except Exception as e:
        log.exception('File processing error')
        return jsonify(error=f'No pude procesar el archivo: {e}'),500

@app.route('/api/capacidades', methods=['GET'])
def api_capacidades():
    if not autorizado(): return jsonify(error='Clave incorrecta.'), 401
    return jsonify(version='4.1', modulos=['chat','memoria','excel','csv','pdf','word','powerpoint','dashboard','dashboard_multi','landing_page','exportaciones','procesamiento_archivos','archivos_descargables'])


@app.route('/api/analisis/multiple', methods=['POST'])
def api_analisis_multiple():
    if not autorizado(): return jsonify(error='Clave incorrecta.'), 401
    try:
        files = request.files.getlist('archivos')
        if not files: return jsonify(error='Adjunta al menos un archivo.'), 400
        out=[]
        for f in files[:10]:
            raw=f.read(); _,_,a=extraer_texto(f.filename, raw); out.append(a)
        roles={}
        for a in out:
            headers=sum([h.get('headers',[]) for h in a.get('hojas',[])], [])
            for k,v in _infer_roles(headers).items(): roles.setdefault(k,[]).extend(v)
        return jsonify(archivos=out,total_archivos=len(out),roles={k:list(dict.fromkeys(v)) for k,v in roles.items()})
    except Exception as e:
        log.exception('Multi analysis error'); return jsonify(error=str(e)), 400


@app.route('/api/dashboard/multi', methods=['POST'])
def api_dashboard_multi():
    if not autorizado(): return jsonify(error='Clave incorrecta.'), 401
    try:
        files=request.files.getlist('archivos')
        ids=request.form.getlist('file_ids')
        items=[]
        for fa in request.files.getlist('analisis')[:10]:
            try: items.append(_leer_analisis(fa))
            except Exception: log.exception('Análisis inválido en dashboard multi')
        files=files[:max(0,10-len(items))]
        for f in files[:10]:
            ext=f.filename.lower().rsplit('.',1)[-1] if '.' in f.filename else ''
            if ext not in ('xlsx','xlsm','xls','csv'): continue
            raw=f.read(); _,_,a=extraer_texto(f.filename,raw); items.append(a)
        for fid in ids[:max(0,10-len(items))]:
            path=_artifact_path(fid)
            if path:
                raw=path.read_bytes(); nombre=path.name.split('_',1)[1] if '_' in path.name else path.name
                ext=nombre.lower().rsplit('.',1)[-1] if '.' in nombre else ''
                if ext in ('xlsx','xlsm','xls','csv'):
                    _,_,a=extraer_texto(nombre,raw); items.append(a)
        if not items: return jsonify(error='Adjunta al menos un Excel o CSV.'),400
        if not items: return jsonify(error='No se encontraron archivos Excel/CSV compatibles.'),400
        html=_multi_dashboard_html(items)
        return send_file(io.BytesIO(html.encode('utf-8')),as_attachment=True,download_name='TAMPY_dashboard_multiarchivo.html',mimetype='text/html; charset=utf-8')
    except Exception as e:
        log.exception('Multi dashboard error'); return jsonify(error=str(e)),500


@app.route('/api/landing', methods=['POST'])
def api_landing():
    if not autorizado(): return jsonify(error='Clave incorrecta.'),401
    d=request.get_json(silent=True) or {}
    prompt=str(d.get('prompt','')).strip(); nombre=str(d.get('nombre','Mi negocio')).strip()[:120]; estilo=str(d.get('estilo','moderno, elegante y responsive')).strip()[:300]
    if not prompt: return jsonify(error='Describe la landing que quieres crear.'),400
    html=None
    if client:
        system=('Eres un desarrollador frontend senior. Genera SOLO un archivo HTML completo y autónomo, listo para publicar. Incluye HTML, CSS y JavaScript en el mismo archivo, responsive, accesible, SEO básico y animaciones suaves. No uses Markdown ni bloques de código. No expliques nada.')
        user=f'Negocio: {nombre}\nEstilo: {estilo}\nRequisitos: {prompt}\nCrea una landing page premium.'
        try:
            r=client.chat.completions.create(model=MODELOS_TEXTO[0],messages=[{'role':'system','content':system},{'role':'user','content':user}],temperature=.7,max_tokens=7000)
            html=(r.choices[0].message.content or '').strip()
            html=re.sub(r'^```(?:html)?\s*|\s*```$','',html,flags=re.I|re.S).strip()
        except Exception: html=None
    if not html or '<html' not in html.lower(): html=_landing_fallback(nombre)
    safe=re.sub(r'[^A-Za-z0-9_-]','_',nombre)[:60] or 'landing'
    return send_file(io.BytesIO(html.encode('utf-8')),as_attachment=True,download_name=f'{safe}_landing.html',mimetype='text/html; charset=utf-8')

@app.route('/api/dashboard',methods=['POST'])
def api_dashboard():
    if not autorizado(): return jsonify(error='Clave incorrecta.'),401
    try:
        f=request.files.get('archivo')
        file_id=(request.form.get('file_id') or '').strip()
        fa=request.files.get('analisis'); a=None
        if fa:
            a=_leer_analisis(fa); nombre=a['nombre']
        elif f and f.filename:
            raw=f.read(); nombre=f.filename
        elif file_id:
            path=_artifact_path(file_id)
            if not path: return jsonify(error='El archivo ya no está disponible en esta instancia. Vuelve a subirlo.'),404
            raw=path.read_bytes(); nombre=path.name.split('_',1)[1] if '_' in path.name else path.name
        else:
            return jsonify(error='Adjunta un Excel o CSV.'),400
        ext=nombre.lower().rsplit('.',1)[-1]
        if ext not in ('xlsx','xlsm','xls','csv'): return jsonify(error='El dashboard automático requiere XLSX, XLSM, XLS o CSV.'),400
        if a is None: _,_,a=extraer_texto(nombre,raw)
        html=_dashboard_html(a)
        bio=io.BytesIO(html.encode('utf-8'))
        return send_file(bio,as_attachment=True,download_name=re.sub(r'[^A-Za-z0-9_-]','_',Path(nombre).stem) + '_dashboard.html',mimetype='text/html; charset=utf-8')
    except Exception as e:
        log.exception('Dashboard error'); return jsonify(error=str(e)),500


@app.route('/api/dashboard.zip',methods=['POST'])
def api_dashboard_zip():
    if not autorizado(): return jsonify(error='Clave incorrecta.'),401
    import zipfile
    try:
        f=request.files.get('archivo'); file_id=(request.form.get('file_id') or '').strip()
        fa=request.files.get('analisis'); a=None
        if fa:
            a=_leer_analisis(fa); nombre=a['nombre']
        elif f and f.filename:
            raw=f.read(); nombre=f.filename
        elif file_id:
            path=_artifact_path(file_id)
            if not path: return jsonify(error='El archivo ya no está disponible. Vuelve a subirlo.'),404
            raw=path.read_bytes(); nombre=path.name.split('_',1)[1] if '_' in path.name else path.name
        else: return jsonify(error='Adjunta un Excel o CSV.'),400
        ext=nombre.lower().rsplit('.',1)[-1]
        if ext not in ('xlsx','xlsm','xls','csv'): return jsonify(error='El ZIP de dashboard requiere XLSX, XLS, XLSM o CSV.'),400
        if a is None: _,_,a=extraer_texto(nombre,raw)
        dashboard=_dashboard_html(a)
        bio=io.BytesIO()
        with zipfile.ZipFile(bio,'w',zipfile.ZIP_DEFLATED) as z:
            base=re.sub(r'[^A-Za-z0-9_-]','_',Path(nombre).stem)
            z.writestr(base+'_dashboard.html',dashboard)
            z.writestr('README.txt','Dashboard generado por TAMPY PRO. El HTML es autónomo y puede abrirse directamente en un navegador.')
        bio.seek(0)
        return send_file(bio,as_attachment=True,download_name=re.sub(r'[^A-Za-z0-9_-]','_',Path(nombre).stem)+'_dashboard.zip',mimetype='application/zip')
    except Exception as e:
        log.exception('Dashboard ZIP error'); return jsonify(error=str(e)),500


@app.route('/api/chat',methods=['POST'])
def api_chat():
    if not autorizado(): return jsonify(error='Clave incorrecta.'),401
    if client is None: return jsonify(error='Falta GROQ_API_KEY.'),500
    try:
        mensajes=limpiar_mensajes((request.get_json(silent=True) or {}).get('mensajes'))
        # Explicit memory command: store and also continue the conversation.
        for item in mensajes:
            if item['role']=='user' and isinstance(item['content'],str):
                for content,cat in extract_memory_candidates(item['content']):
                    try: add_memory(content,cat)
                    except Exception: log.exception('No se pudo guardar una memoria; continúo con la respuesta')
        texto,modelo=consultar(mensajes)
        return jsonify(respuesta=texto,modelo=modelo)
    except Exception as e:
        log.exception('Error /api/chat'); t=str(e).lower()
        if ('rate' in t and 'limit' in t) or '429' in t: return jsonify(error='Llegaste al límite de Groq. Intenta de nuevo en un momento.'),429
        if 'invalid api key' in t or '401' in t: return jsonify(error='La API key de Groq es inválida o fue revocada.'),500
        return jsonify(error=f'Error del modelo: {e}'),500


def md_to_plain(md):
    s=re.sub(r'```[\s\S]*?```',lambda m:'\n'+m.group(0)+'\n',md)
    s=re.sub(r'[#*_>`]','',s); return s

# ================= Exportadores de archivos (Markdown -> Word / PDF / Excel) =================
def _es_sep_md(l):
    return bool(re.match(r'^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$', l))

def _fila_md(l):
    l=l.strip()
    if l.startswith('|'): l=l[1:]
    if l.endswith('|'): l=l[:-1]
    return [c.strip() for c in l.split('|')]

def _tablas_desde_texto(text):
    """Devuelve [(titulo, filas)] a partir de tablas Markdown. Cada título (#) que precede a una tabla es una hoja."""
    tablas=[]; titulo=None; cur=None
    for ln in text.splitlines():
        sl=ln.strip()
        m=re.match(r'^#{1,6}\s+(.*)',sl)
        if m:
            if cur: tablas.append((titulo,cur)); cur=None; titulo=None
            titulo=re.sub(r'[*`]','',m.group(1)).strip(); continue
        if sl.count('|')>=2:
            if _es_sep_md(sl): continue
            if cur is None: cur=[]
            cur.append(_fila_md(sl))
        else:
            if cur: tablas.append((titulo,cur)); cur=None; titulo=None
    if cur: tablas.append((titulo,cur))
    return tablas

def _filas_csv(text):
    import csv as _csv
    lines=[l for l in text.splitlines() if l.strip()]
    if len(lines)<2: return None
    for d in (',',';','\t'):
        try: filas=list(_csv.reader(lines,delimiter=d))
        except Exception: continue
        lens=[len(f) for f in filas]
        if not lens: continue
        moda=max(set(lens),key=lens.count)
        if moda>=2 and lens.count(moda)>=max(2,int(len(filas)*0.8)): return filas
    return None

def _num_celda(v):
    s=str(v).strip()
    if not s: return v
    t=s.replace('$','').replace(' ','')
    if re.match(r'^-?0\d',t) or re.match(r'^-?\d{12,}$',t): return v   # códigos y códigos de barras: se quedan como texto
    if re.match(r'^-?\d+(\.\d+)?$',t): return float(t) if '.' in t else int(t)
    if re.match(r'^-?\d{1,3}(,\d{3})+(\.\d+)?$',t):
        t2=t.replace(',',''); return float(t2) if '.' in t2 else int(t2)
    if re.match(r'^-?\d+,\d{1,2}$',t): return float(t.replace(',','.'))
    return v

def _runs_docx(par, txt):
    for part in re.split(r'(\*\*[^*]+\*\*|`[^`]+`|(?<!\*)\*[^*\n]+\*(?!\*))', txt):
        if not part: continue
        if part.startswith('**') and part.endswith('**') and len(part)>4: r=par.add_run(part[2:-2]); r.bold=True
        elif part.startswith('`') and part.endswith('`') and len(part)>2: r=par.add_run(part[1:-1]); r.font.name='Consolas'
        elif part.startswith('*') and part.endswith('*') and len(part)>2: r=par.add_run(part[1:-1]); r.italic=True
        else: par.add_run(part)

def _md_a_docx(doc, text):
    from docx.shared import Pt
    lines=text.splitlines(); i=0; codigo=False
    while i<len(lines):
        raw=lines[i]; sl=raw.strip(); i+=1
        if sl.startswith('```'): codigo=not codigo; continue
        if codigo:
            p=doc.add_paragraph(); r=p.add_run(raw); r.font.name='Consolas'; r.font.size=Pt(9); continue
        if not sl or re.match(r'^(-{3,}|\*{3,}|_{3,})$',sl): continue
        if sl.count('|')>=2:
            filas=[]; j=i-1
            while j<len(lines) and lines[j].count('|')>=2:
                if not _es_sep_md(lines[j].strip()): filas.append(_fila_md(lines[j]))
                j+=1
            i=j
            if filas:
                n=max(len(f) for f in filas); t=doc.add_table(rows=len(filas),cols=n); t.style='Table Grid'
                for rr,fila in enumerate(filas):
                    for cc in range(n):
                        cell=t.cell(rr,cc); cell.text=''; p=cell.paragraphs[0]; _runs_docx(p, fila[cc] if cc<len(fila) else '')
                        if rr==0:
                            for run in p.runs: run.bold=True
                doc.add_paragraph()
            continue
        m=re.match(r'^(#{1,6})\s+(.*)',sl)
        if m: doc.add_heading(re.sub(r'[*`]','',m.group(2)).strip(), level=min(len(m.group(1)),3)); continue
        m=re.match(r'^\s*[-*+]\s+(.*)',raw)
        if m: _runs_docx(doc.add_paragraph(style='List Bullet'),m.group(1)); continue
        m=re.match(r'^\s*\d+[.)]\s+(.*)',raw)
        if m: _runs_docx(doc.add_paragraph(style='List Number'),m.group(1)); continue
        m=re.match(r'^>\s?(.*)',sl)
        _runs_docx(doc.add_paragraph(), m.group(1) if m else sl)

def _md_a_pdf(text):
    import textwrap
    from xml.sax.saxutils import escape
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, XPreformatted
    limpiar=lambda t: t.encode('cp1252','ignore').decode('cp1252')
    def inline(t):
        t=escape(limpiar(t))
        t=re.sub(r'\*\*([^*]+)\*\*',r'<b>\1</b>',t)
        t=re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)',r'<i>\1</i>',t)
        return re.sub(r'`([^`]+)`',r'<font face="Courier">\1</font>',t)
    st=getSampleStyleSheet()
    body=st['BodyText']; liste=ParagraphStyle('li',parent=body,leftIndent=16,bulletIndent=4)
    celda=ParagraphStyle('ce',parent=body,fontSize=8.5,leading=10.5)
    codest=ParagraphStyle('co',parent=body,fontName='Courier',fontSize=7.5,leading=9.5,backColor=colors.HexColor('#f1f5f9'),leftIndent=4)
    bio=io.BytesIO(); doc=SimpleDocTemplate(bio,pagesize=A4,rightMargin=40,leftMargin=40,topMargin=40,bottomMargin=40)
    ancho=A4[0]-80; story=[]; lines=text.splitlines(); i=0; codigo=False; buf=[]
    while i<len(lines):
        raw=lines[i]; sl=raw.strip(); i+=1
        if sl.startswith('```'):
            if codigo and buf:
                bloque='\n'.join(textwrap.fill(l,100,replace_whitespace=False,drop_whitespace=False) if len(l)>100 else l for l in buf)
                story.append(XPreformatted(escape(limpiar(bloque)),codest)); story.append(Spacer(1,6)); buf=[]
            codigo=not codigo; continue
        if codigo: buf.append(raw); continue
        if not sl: story.append(Spacer(1,6)); continue
        if re.match(r'^(-{3,}|\*{3,}|_{3,})$',sl): continue
        if sl.count('|')>=2:
            filas=[]; j=i-1
            while j<len(lines) and lines[j].count('|')>=2:
                if not _es_sep_md(lines[j].strip()): filas.append(_fila_md(lines[j]))
                j+=1
            i=j
            if filas:
                n=max(len(f) for f in filas)
                data=[[Paragraph(('<b>%s</b>' if ri==0 else '%s') % inline(f[c] if c<len(f) else ''),celda) for c in range(n)] for ri,f in enumerate(filas)]
                t=Table(data,colWidths=[ancho/n]*n,repeatRows=1)
                t.setStyle(TableStyle([('GRID',(0,0),(-1,-1),.4,colors.HexColor('#94a3b8')),('BACKGROUND',(0,0),(-1,0),colors.HexColor('#dbeafe')),('VALIGN',(0,0),(-1,-1),'TOP')]))
                story.append(t); story.append(Spacer(1,8))
            continue
        m=re.match(r'^(#{1,6})\s+(.*)',sl)
        if m: story.append(Paragraph(inline(m.group(2)),st['Heading%d'%min(len(m.group(1)),3)])); continue
        m=re.match(r'^\s*[-*+]\s+(.*)',raw)
        if m: story.append(Paragraph(inline(m.group(1)),liste,bulletText='•')); continue
        m=re.match(r'^\s*(\d+)[.)]\s+(.*)',raw)
        if m: story.append(Paragraph(inline(m.group(2)),liste,bulletText=m.group(1)+'.')); continue
        m=re.match(r'^>\s?(.*)',sl)
        story.append(Paragraph(inline(m.group(1) if m else sl),body)); story.append(Spacer(1,3))
    if codigo and buf: story.append(XPreformatted(escape(limpiar('\n'.join(buf))),codest))
    if not story: story.append(Paragraph(' ',body))
    doc.build(story); bio.seek(0); return bio

def _md_a_xlsx(text):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    limpio=lambda v: re.sub(r'(\*\*|`)','',str(v))
    tablas=_tablas_desde_texto(text)
    if not tablas:
        f=_filas_csv(text)
        if f: tablas=[(None,f)]
    wb=Workbook(); wb.remove(wb.active)
    if not tablas:   # texto sin tablas: una línea por fila
        ws=wb.create_sheet('TAMPY')
        for i,line in enumerate(text.splitlines(),1): ws.cell(i,1,md_to_plain(line))
        ws.column_dimensions['A'].width=120
    usados=set()
    for k,(tit,filas) in enumerate(tablas,1):
        base=(re.sub(r'[\\/*?:\[\]]','',tit or '').strip() or f'Hoja{k}')[:28]; nombre=base; z=2
        while nombre in usados: nombre=f'{base[:25]}_{z}'; z+=1
        usados.add(nombre); ws=wb.create_sheet(nombre)
        for r,fila in enumerate(filas,1):
            for c,v in enumerate(fila,1):
                v=limpio(v); ws.cell(r,c,_num_celda(v) if r>1 else v)
        for c in range(1,max(len(f) for f in filas)+1):
            ws.cell(1,c).font=Font(bold=True,color='FFFFFF'); ws.cell(1,c).fill=PatternFill('solid',fgColor='0A6FC2')
            ws.cell(1,c).alignment=Alignment(vertical='center')
            mx=max((len(str(ws.cell(r,c).value or '')) for r in range(1,min(len(filas),200)+1)),default=8)
            ws.column_dimensions[get_column_letter(c)].width=min(max(mx+2,10),60)
        ws.freeze_panes='A2'
        if len(filas)>1: ws.auto_filter.ref=ws.dimensions
    bio=io.BytesIO(); wb.save(bio); bio.seek(0); return bio

@app.route('/api/export',methods=['POST'])
def api_export():
    if not autorizado(): return jsonify(error='Clave incorrecta.'),401
    d=request.get_json(silent=True) or {}; text=str(d.get('text','')); fmt=str(d.get('format','txt')).lower()
    if not text: return jsonify(error='No hay contenido para exportar.'),400
    name=re.sub(r'[^A-Za-z0-9_-]','_',str(d.get('name','TAMPY_documento')))[:60]
    try:
        if fmt=='txt':
            bio=io.BytesIO(text.encode('utf-8')); mime='text/plain; charset=utf-8'; ext='txt'
        elif fmt=='docx':
            from docx import Document
            doc=Document(); _md_a_docx(doc,text)
            bio=io.BytesIO(); doc.save(bio); bio.seek(0); mime='application/vnd.openxmlformats-officedocument.wordprocessingml.document'; ext='docx'
        elif fmt=='pdf':
            bio=_md_a_pdf(text); mime='application/pdf'; ext='pdf'
        elif fmt=='html':
            from markupsafe import escape as _esc
            body=md_to_plain(text).replace('\n','<br>')
            html=f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_esc(name)}</title><style>body{{font-family:system-ui;max-width:900px;margin:40px auto;padding:0 20px;line-height:1.7;color:#172033}}h1{{border-bottom:2px solid #315efb;padding-bottom:10px}}pre{{background:#101828;color:white;padding:16px;border-radius:12px;overflow:auto}}</style></head><body><h1>{_esc(name)}</h1><div>{body}</div><footer style="margin-top:40px;color:#667085">Generado por TAMPY PRO</footer></body></html>'''
            bio=io.BytesIO(html.encode('utf-8')); mime='text/html; charset=utf-8'; ext='html'
        elif fmt=='xlsx':
            bio=_md_a_xlsx(text); mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'; ext='xlsx'
        elif fmt=='pptx':
            from pptx import Presentation
            from pptx.util import Pt
            slides=[]; cur=None
            for line in text.splitlines():
                ln=line.strip()
                if not ln or re.match(r'^```',ln): continue
                m=re.match(r'^#{1,3}\s+(.*)',ln)
                if m:
                    cur={'t':md_to_plain(m.group(1)).strip() or 'TAMPY','b':[]}; slides.append(cur)
                else:
                    if cur is None:
                        cur={'t':name.replace('_',' '),'b':[]}; slides.append(cur)
                    cur['b'].append(md_to_plain(re.sub(r'^([-*+]|\d+[.)])\s+','',ln)).strip())
            if not slides: return jsonify(error='No hay contenido para la presentación.'),400
            prs=Presentation()
            for sl in slides:
                partes=[sl['b'][i:i+6] for i in range(0,len(sl['b']),6)] or [[]]
                for n,grupo in enumerate(partes):
                    sd=prs.slides.add_slide(prs.slide_layouts[1])
                    sd.shapes.title.text=sl['t']+(f' ({n+1})' if n else '')
                    tf=sd.placeholders[1].text_frame
                    for i,b in enumerate(grupo):
                        para=tf.paragraphs[0] if i==0 else tf.add_paragraph()
                        para.text=b
                        for r in para.runs: r.font.size=Pt(20)
            bio=io.BytesIO(); prs.save(bio); bio.seek(0); mime='application/vnd.openxmlformats-officedocument.presentationml.presentation'; ext='pptx'
        else: return jsonify(error='Formato no soportado.'),400
        return send_file(bio,as_attachment=True,download_name=f'{name}.{ext}',mimetype=mime)
    except Exception as e:
        log.exception('Export error'); return jsonify(error=str(e)),500


if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)),debug=False)

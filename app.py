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

from flask import Flask, jsonify, render_template, request, send_file
from groq import Groq

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tampy")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024

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
    "Si una memoria contradice el mensaje actual del usuario, prioriza el mensaje actual."
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

def analizar_excel(nombre, datos):
    """Analiza XLSX/XLSM localmente sin enviar el libro completo a Groq."""
    from openpyxl import load_workbook
    libro = load_workbook(io.BytesIO(datos), read_only=True, data_only=True)
    hojas = []
    contexto = [f"ARCHIVO EXCEL: {nombre}", f"HOJAS: {len(libro.worksheets)}"]
    total_filas = 0
    for hoja in list(libro.worksheets)[:20]:
        rows = []
        nonempty = 0
        headers = []
        for i, fila in enumerate(hoja.iter_rows(values_only=True)):
            vals = [_valor_texto(v) for v in fila]
            if not any(vals):
                continue
            if i == 0:
                headers = vals
            rows.append(vals)
            nonempty += 1
            if len(rows) >= MAX_FILAS_ANALISIS:
                break
        data_rows = rows[1:] if headers else rows
        if not headers and rows:
            headers = [f"Columna {i+1}" for i in range(len(rows[0]))]
        ncols = max([len(headers)] + [len(r) for r in rows] + [0])
        headers = (headers + [f"Columna {i+1}" for i in range(ncols-len(headers))])[:ncols]
        numeric = []
        for j in range(ncols):
            nums=[]
            for r in data_rows:
                if j < len(r):
                    try:
                        x=float(str(r[j]).replace(",", "").replace("$","").strip())
                        nums.append(x)
                    except Exception:
                        pass
            if len(nums) >= max(3, int(len(data_rows)*0.5)):
                numeric.append({
                    "column": headers[j] or f"Columna {j+1}",
                    "count": len(nums),
                    "sum": round(sum(nums),2),
                    "avg": round(sum(nums)/len(nums),2),
                    "min": round(min(nums),2),
                    "max": round(max(nums),2)
                })
        categorical=[]
        for j in range(min(ncols,30)):
            vals=[r[j] for r in data_rows if j<len(r) and r[j]!=""]
            if vals and len(set(vals)) <= min(100, max(10,int(len(vals)*.5))):
                freq={}
                for v in vals: freq[v]=freq.get(v,0)+1
                top=sorted(freq.items(),key=lambda x:x[1],reverse=True)[:10]
                categorical.append({"column":headers[j] or f"Columna {j+1}","unique":len(set(vals)),"top":[{"value":str(k),"count":v} for k,v in top]})
        muestra=[dict(zip(headers, r + [""]*(ncols-len(r)))) for r in data_rows[:30]]
        hoja_info={"name":hoja.title,"rows":len(data_rows),"columns":ncols,"headers":headers,"numeric":numeric[:15],"categorical":categorical[:15],"sample":muestra}
        hojas.append(hoja_info)
        total_filas += len(data_rows)
        contexto.append(f"Hoja '{hoja.title}': {len(data_rows)} filas, {ncols} columnas.")
        if numeric:
            contexto.append("  Métricas: " + "; ".join(f"{x['column']} suma={x['sum']} promedio={x['avg']} min={x['min']} max={x['max']}" for x in numeric[:8]))
        if categorical:
            contexto.append("  Categorías: " + "; ".join(f"{x['column']} únicos={x['unique']}" for x in categorical[:6]))
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
            r=client.chat.completions.create(model=modelo,messages=[system]+mensajes,temperature=.6,max_tokens=2500)
            texto=(r.choices[0].message.content or '').strip(); texto=re.sub(r'<think>.*?</think>','',texto,flags=re.S).strip()
            return texto or 'No obtuve respuesta del modelo.',modelo
        except Exception as e:
            ultimo=e
            if modelo_no_disponible(e): continue
            raise
    raise RuntimeError(f'Ningún modelo configurado está disponible en Groq. Último error: {ultimo}')


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
    if len(raw) > app.config["MAX_CONTENT_LENGTH"]: return jsonify(error='El archivo supera el límite de 30 MB.'),413
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
        for f in files[:10]:
            ext=f.filename.lower().rsplit('.',1)[-1] if '.' in f.filename else ''
            if ext not in ('xlsx','xlsm','xls','csv'): continue
            raw=f.read(); _,_,a=extraer_texto(f.filename,raw); items.append(a)
        for fid in ids[:10-len(items)]:
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
        if f and f.filename:
            raw=f.read(); nombre=f.filename
        elif file_id:
            path=_artifact_path(file_id)
            if not path: return jsonify(error='El archivo ya no está disponible en esta instancia. Vuelve a subirlo.'),404
            raw=path.read_bytes(); nombre=path.name.split('_',1)[1] if '_' in path.name else path.name
        else:
            return jsonify(error='Adjunta un Excel o CSV.'),400
        ext=nombre.lower().rsplit('.',1)[-1]
        if ext not in ('xlsx','xlsm','xls','csv'): return jsonify(error='El dashboard automático requiere XLSX, XLSM, XLS o CSV.'),400
        _,_,a=extraer_texto(nombre,raw)
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
        if f and f.filename:
            raw=f.read(); nombre=f.filename
        elif file_id:
            path=_artifact_path(file_id)
            if not path: return jsonify(error='El archivo ya no está disponible. Vuelve a subirlo.'),404
            raw=path.read_bytes(); nombre=path.name.split('_',1)[1] if '_' in path.name else path.name
        else: return jsonify(error='Adjunta un Excel o CSV.'),400
        ext=nombre.lower().rsplit('.',1)[-1]
        if ext not in ('xlsx','xlsm','xls','csv'): return jsonify(error='El ZIP de dashboard requiere XLSX, XLS, XLSM o CSV.'),400
        _,_,a=extraer_texto(nombre,raw); dashboard=_dashboard_html(a)
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
            doc=Document();
            for line in text.splitlines(): doc.add_paragraph(md_to_plain(line))
            bio=io.BytesIO(); doc.save(bio); bio.seek(0); mime='application/vnd.openxmlformats-officedocument.wordprocessingml.document'; ext='docx'
        elif fmt=='pdf':
            from reportlab.lib.pagesizes import A4
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
            from reportlab.lib.styles import getSampleStyleSheet
            from xml.sax.saxutils import escape
            bio=io.BytesIO(); doc=SimpleDocTemplate(bio,pagesize=A4,rightMargin=40,leftMargin=40,topMargin=40,bottomMargin=40); styles=getSampleStyleSheet(); story=[]
            for line in text.splitlines():
                line=line.strip()
                if not line: story.append(Spacer(1,8)); continue
                story.append(Paragraph(escape(md_to_plain(line)),styles['BodyText'])); story.append(Spacer(1,4))
            doc.build(story); bio.seek(0); mime='application/pdf'; ext='pdf'
        elif fmt=='html':
            from markupsafe import escape as _esc
            body=md_to_plain(text).replace('\n','<br>')
            html=f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_esc(name)}</title><style>body{{font-family:system-ui;max-width:900px;margin:40px auto;padding:0 20px;line-height:1.7;color:#172033}}h1{{border-bottom:2px solid #315efb;padding-bottom:10px}}pre{{background:#101828;color:white;padding:16px;border-radius:12px;overflow:auto}}</style></head><body><h1>{_esc(name)}</h1><div>{body}</div><footer style="margin-top:40px;color:#667085">Generado por TAMPY PRO</footer></body></html>'''
            bio=io.BytesIO(html.encode('utf-8')); mime='text/html; charset=utf-8'; ext='html'
        elif fmt=='xlsx':
            from openpyxl import Workbook
            wb=Workbook(); ws=wb.active; ws.title='TAMPY';
            for i,line in enumerate(text.splitlines(),1): ws.cell(i,1,md_to_plain(line))
            ws.column_dimensions['A'].width=120; bio=io.BytesIO(); wb.save(bio); bio.seek(0); mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'; ext='xlsx'
        else: return jsonify(error='Formato no soportado.'),400
        return send_file(bio,as_attachment=True,download_name=f'{name}.{ext}',mimetype=mime)
    except Exception as e:
        log.exception('Export error'); return jsonify(error=str(e)),500


if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)),debug=False)

"""Consultorio web - Dra. Natalia Morera. Ejecutar: pip install flask && python app.py
Variables: SECRET_KEY, ADMIN_PASSWORD, DB_PATH, UPLOAD_DIR"""
import os, sqlite3, datetime as dt, urllib.parse as up, time, shutil, re, json, urllib.request as urlreq, calendar, smtplib
from email.mime.text import MIMEText
from functools import wraps
from flask import (Flask, g, request, redirect, session, render_template_string,
                   flash, abort, send_from_directory, Response)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("DB_PATH", os.path.join(BASE, "consultorio_web.db"))
UPD = os.environ.get("UPLOAD_DIR", os.path.join(BASE, "radiografias"))
os.makedirs(UPD, exist_ok=True)
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = bool(DATABASE_URL)
if USE_PG:
    import psycopg2
    import psycopg2.extras

# ---- Almacenamiento de archivos (logo, radiografias) en Supabase Storage, para que no se
# borren con cada reinicio de Render. Si no esta configurado, se guarda localmente como antes. ----
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "archivos").strip()
USE_SUPABASE_STORAGE = bool(SUPABASE_URL and SUPABASE_KEY)

def _sb_headers(extra=None):
    # Claves nuevas (sb_secret_...) van solo en apikey; claves antiguas (JWT, eyJ...) tambien en Authorization.
    h = {"apikey": SUPABASE_KEY}
    if not SUPABASE_KEY.startswith("sb_"):
        h["Authorization"] = f"Bearer {SUPABASE_KEY}"
    if extra:
        h.update(extra)
    return h

def storage_guardar(nombre, datos_bytes, content_type="application/octet-stream"):
    """Guarda un archivo. Si Supabase Storage esta configurado lo sube alla; si no, lo guarda en disco local (UPD)."""
    if USE_SUPABASE_STORAGE:
        url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{nombre}"
        req = urlreq.Request(url, data=datos_bytes, method="POST", headers=_sb_headers({"Content-Type": content_type, "x-upsert": "true"}))
        try:
            with urlreq.urlopen(req, timeout=20) as resp:
                resp.read()
            return True
        except Exception as e:
            print(f"Error subiendo a Supabase Storage: {e}")
            return False
    else:
        with open(os.path.join(UPD, nombre), "wb") as fh:
            fh.write(datos_bytes)
        return True

def storage_borrar(nombre):
    """Borra un archivo (Supabase Storage o disco local). Si falla, se ignora."""
    try:
        if USE_SUPABASE_STORAGE:
            req = urlreq.Request(f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{nombre}",
                                 method="DELETE", headers=_sb_headers())
            urlreq.urlopen(req, timeout=20).read()
        else:
            ruta = os.path.join(UPD, nombre)
            if os.path.exists(ruta): os.remove(ruta)
    except Exception as e:
        print(f"No se pudo borrar archivo {nombre}: {e}")

# ---- Envio de correo (recordatorios de citas) via SMTP. Gratis si usas tu propio correo
# (ej. Gmail con "contraseña de aplicación"). Se puede configurar desde la pantalla de
# Configuracion (se guarda en la tabla settings) o, si se prefiere, con variables de entorno
# en Render (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM_NAME) como respaldo. ----
def _smtp_config():
    """Lee la configuracion SMTP: primero de la base de datos (pantalla Configuracion),
    y si no hay nada guardado ahi, de las variables de entorno de Render."""
    def val(key, env_name, default=""):
        fila = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
        v = (fila["value"] if fila else "").strip() if fila else ""
        return v if v else os.environ.get(env_name, default).strip()
    host = val("smtp_host", "SMTP_HOST")
    puerto_txt = val("smtp_port", "SMTP_PORT", "587")
    user = val("smtp_user", "SMTP_USER")
    passwd = val("smtp_pass", "SMTP_PASS")
    nombre = val("smtp_from_name", "SMTP_FROM_NAME", "Dra. Natalia Morera Odontología Especializada")
    try:
        puerto = int(puerto_txt or 587)
    except ValueError:
        puerto = 587
    return host, puerto, user, passwd, nombre

def email_listo():
    host, puerto, user, passwd, nombre = _smtp_config()
    return bool(host and user and passwd)

def enviar_correo(destinatario, asunto, cuerpo):
    """Envia un correo simple de texto plano. Devuelve (ok, mensaje_error)."""
    host, puerto, user, passwd, nombre = _smtp_config()
    if not (host and user and passwd):
        return False, "El envío de correo no está configurado. Ve a Configuración y completa los datos de correo."
    if not destinatario:
        return False, "El paciente no tiene correo registrado."
    try:
        msg = MIMEText(cuerpo, "plain", "utf-8")
        msg["Subject"] = asunto
        msg["From"] = f"{nombre} <{user}>"
        msg["To"] = destinatario
        with smtplib.SMTP(host, puerto, timeout=15) as server:
            server.starttls()
            server.login(user, passwd)
            server.sendmail(user, [destinatario], msg.as_string())
        return True, None
    except Exception as e:
        return False, str(e)

def storage_leer(nombre):
    """Devuelve (bytes, encontrado) de un archivo, desde Supabase Storage o disco local segun corresponda."""
    if USE_SUPABASE_STORAGE:
        url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{nombre}"
        req = urlreq.Request(url, headers=_sb_headers())
        try:
            with urlreq.urlopen(req, timeout=20) as resp:
                return resp.read(), True
        except Exception:
            return None, False
    else:
        ruta = os.path.join(UPD, nombre)
        if os.path.exists(ruta):
            with open(ruta, "rb") as fh:
                return fh.read(), True
        return None, False
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cambiar-esta-clave")
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
ROLES = ["admin", "odontologo", "radiologo", "recepcion"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, usuario TEXT UNIQUE, nombre TEXT, rol TEXT, clave TEXT, activo INT DEFAULT 1);
CREATE TABLE IF NOT EXISTS patients(id INTEGER PRIMARY KEY, nombre TEXT, documento TEXT, tipo_doc TEXT DEFAULT 'CC', fecha_nac TEXT, sexo TEXT DEFAULT 'F', telefono TEXT, email TEXT, notas TEXT, creado TEXT);
CREATE TABLE IF NOT EXISTS citas(id INTEGER PRIMARY KEY, patient_id INT, nombre_libre TEXT, telefono_libre TEXT, fecha TEXT, motivo TEXT, estado TEXT DEFAULT 'pendiente', origen TEXT DEFAULT 'interno');
CREATE TABLE IF NOT EXISTS plan(id INTEGER PRIMARY KEY, patient_id INT, diente TEXT, tratamiento TEXT, etapa TEXT, costo REAL, estado TEXT DEFAULT 'pendiente', orden INT DEFAULT 0, fecha_prog TEXT, fecha_hecho TEXT, notas TEXT);
CREATE TABLE IF NOT EXISTS labs(id INTEGER PRIMARY KEY, patient_id INT, laboratorio TEXT, trabajo TEXT, enviado TEXT, entrega TEXT, estado TEXT DEFAULT 'enviado');
CREATE TABLE IF NOT EXISTS recetas(id INTEGER PRIMARY KEY, patient_id INT, fecha TEXT, autor TEXT, diagnostico TEXT, folio TEXT);
CREATE TABLE IF NOT EXISTS receta_items(id INTEGER PRIMARY KEY, receta_id INT, medicamento TEXT, dosis TEXT, via TEXT, frecuencia TEXT, duracion TEXT, indicaciones TEXT);
CREATE TABLE IF NOT EXISTS rx(id INTEGER PRIMARY KEY, patient_id INT, archivo TEXT, descripcion TEXT, subido_por TEXT, fecha TEXT);
CREATE TABLE IF NOT EXISTS auditoria(id INTEGER PRIMARY KEY, fecha TEXT, usuario TEXT, accion TEXT, detalle TEXT, ip TEXT);
CREATE TABLE IF NOT EXISTS caja(id INTEGER PRIMARY KEY, fecha_apertura TEXT, monto_apertura REAL, usuario_abre TEXT, fecha_cierre TEXT, monto_cierre REAL, usuario_cierra TEXT, estado TEXT DEFAULT 'abierta');
CREATE TABLE IF NOT EXISTS caja_mov(id INTEGER PRIMARY KEY, caja_id INT, fecha TEXT, tipo TEXT, concepto TEXT, monto REAL, usuario TEXT, patient_id INT);
CREATE TABLE IF NOT EXISTS atenciones(id INTEGER PRIMARY KEY, patient_id INT, fecha TEXT, hora TEXT, usuario TEXT,
    cod_cups TEXT, desc_cups TEXT, cod_cie10 TEXT, desc_cie10 TEXT, tipo_diagnostico TEXT, valor REAL,
    num_factura TEXT, num_autorizacion TEXT, modalidad TEXT, finalidad TEXT, via_ingreso TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS rips_lotes(id INTEGER PRIMARY KEY, fecha TEXT, desde TEXT, hasta TEXT, archivo TEXT, n_usuarios INT, n_registros INT, usuario TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, kind TEXT, price REAL, cost REAL DEFAULT 0, stock INTEGER, min INTEGER, barcode TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS sales(id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id INTEGER, user_id INTEGER, date TEXT, subtotal REAL, discount REAL, tax REAL, total REAL, paid REAL, method TEXT, items TEXT, voided INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS einvoices(id INTEGER PRIMARY KEY AUTOINCREMENT, sale_id INTEGER, number TEXT, cufe TEXT, xml TEXT, status TEXT, response TEXT, created TEXT, sent_at TEXT, attempts INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS expenses(id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, concept TEXT, amount REAL, user_id INTEGER);
CREATE TABLE IF NOT EXISTS purchases(id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, product_id INTEGER, qty INTEGER, cost REAL, supplier TEXT, user_id INTEGER);
CREATE TABLE IF NOT EXISTS dentists(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, specialty TEXT, license TEXT, phone TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS services(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, category TEXT, price REAL, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id INTEGER, usuario TEXT, fecha TEXT, texto TEXT);
CREATE TABLE IF NOT EXISTS quotes(id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id INTEGER, fecha TEXT, items TEXT, total REAL, usuario TEXT);
CREATE TABLE IF NOT EXISTS abonos(id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id INTEGER, fecha TEXT, monto REAL, concepto TEXT, usuario TEXT);
CREATE TABLE IF NOT EXISTS consentimientos(id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id INTEGER, fecha TEXT, procedimiento TEXT, texto TEXT, firmado_por TEXT, usuario TEXT);
CREATE TABLE IF NOT EXISTS cuentas(id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id INTEGER, plan_id INTEGER, fecha TEXT, descripcion TEXT, total REAL, abonado REAL DEFAULT 0, fecha_vence TEXT, estado TEXT DEFAULT 'abierta');
CREATE TABLE IF NOT EXISTS documentos(id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id INTEGER, archivo TEXT, nombre_original TEXT, descripcion TEXT, subido_por TEXT, fecha TEXT);
CREATE TABLE IF NOT EXISTS informes(id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id INTEGER, fecha TEXT, titulo TEXT, contenido TEXT, autor TEXT, folio TEXT);
"""

def _pg_sql(sql):
    # Traduce SQLite -> PostgreSQL: placeholders, tipos, e INSERT OR REPLACE
    s = sql
    s = s.replace("INSERT OR REPLACE INTO", "__UPSERT__")
    s = re.sub(r"\?", "%s", s)
    # SQLite GROUP_CONCAT -> PostgreSQL STRING_AGG
    s = re.sub(r"GROUP_CONCAT\(\s*([^,()]+?)\s*,\s*('[^']*')\s*\)", r"STRING_AGG(\1::text, \2)", s, flags=re.I)
    s = re.sub(r"GROUP_CONCAT\(\s*([^,()]+?)\s*\)", r"STRING_AGG(\1::text, ',')", s, flags=re.I)
    if s.strip().startswith("__UPSERT__"):
        tabla = s.split("__UPSERT__", 1)[1].strip().split("(", 1)[0].strip()
        s = s.replace("__UPSERT__" + " " + tabla, f"INSERT INTO {tabla}") if ("__UPSERT__ " + tabla) in s else s.replace("__UPSERT__", "INSERT INTO")
        if tabla == "settings":
            s = s.rstrip().rstrip(";") + " ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value"
    s = s.replace("AUTOINCREMENT", "")
    return s

def db():
    if "db" not in g:
        if USE_PG:
            g.db = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close(_):
    d = g.pop("db", None)
    if d: d.close()

def q(sql, a=(), one=False):
    conn = db()
    if USE_PG:
        cur = conn.cursor()
        cur.execute(_pg_sql(sql), tuple(a))
        r = cur.fetchall()
        cur.close()
    else:
        r = conn.execute(sql, a).fetchall()
    return (r[0] if r else None) if one else r

def ex(sql, a=()):
    conn = db()
    if USE_PG:
        cur = conn.cursor()
        s = _pg_sql(sql)
        _m = re.match(r"\s*INSERT\s+INTO\s+(\w+)", s, re.I)
        _sin_id = bool(_m) and _m.group(1).lower() in ("settings",)
        if s.strip().upper().startswith("INSERT") and "RETURNING" not in s.upper() and not _sin_id:
            s = s.rstrip().rstrip(";") + " RETURNING id"
            cur.execute(s, tuple(a))
            row = cur.fetchone()
            conn.commit()
            cur.close()
            return row["id"] if row else None
        cur.execute(s, tuple(a))
        conn.commit()
        cur.close()
        return None
    else:
        c = conn.execute(sql, a); conn.commit(); return c.lastrowid

def _pg_schema():
    s = SCHEMA
    s = re.sub(r"INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY", s)
    s = re.sub(r"INTEGER PRIMARY KEY(?!\s+AUTOINCREMENT)", "SERIAL PRIMARY KEY", s)
    return s

def init():
    if USE_PG:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute(_pg_schema())
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='plan'")
        cols = [r[0] for r in cur.fetchall()]
        if "condicion" not in cols:
            cur.execute("ALTER TABLE plan ADD COLUMN condicion TEXT DEFAULT 'tratamiento'")
        if "superficie" not in cols:
            cur.execute("ALTER TABLE plan ADD COLUMN superficie TEXT")
        if "profesional" not in cols:
            cur.execute("ALTER TABLE plan ADD COLUMN profesional TEXT")
        if "fecha_registro" not in cols:
            cur.execute("ALTER TABLE plan ADD COLUMN fecha_registro TEXT")
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='patients'")
        pcols = [r[0] for r in cur.fetchall()]
        if "alergias" not in pcols:
            cur.execute("ALTER TABLE patients ADD COLUMN alergias TEXT")
        if "antecedentes" not in pcols:
            cur.execute("ALTER TABLE patients ADD COLUMN antecedentes TEXT")
        if "foto" not in pcols:
            cur.execute("ALTER TABLE patients ADD COLUMN foto TEXT")
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='citas'")
        ccols = [r[0] for r in cur.fetchall()]
        if "dentist_id" not in ccols:
            cur.execute("ALTER TABLE citas ADD COLUMN dentist_id INTEGER")
        cur.execute("SELECT 1 FROM users WHERE usuario='admin'")
        if not cur.fetchone():
            cur.execute("INSERT INTO users(usuario,nombre,rol,clave) VALUES(%s,%s,%s,%s)",
                        ('admin', 'Administrador', 'admin', generate_password_hash(os.environ.get("ADMIN_PASSWORD", "admin123"))))
        conn.commit()
        cur.close(); conn.close()
        return
    with sqlite3.connect(DB) as c:
        c.executescript(SCHEMA)
        cols = [r[1] for r in c.execute("PRAGMA table_info(plan)").fetchall()]
        if "condicion" not in cols:
            c.execute("ALTER TABLE plan ADD COLUMN condicion TEXT DEFAULT 'tratamiento'")
        if "superficie" not in cols:
            c.execute("ALTER TABLE plan ADD COLUMN superficie TEXT")
        if "profesional" not in cols:
            c.execute("ALTER TABLE plan ADD COLUMN profesional TEXT")
        if "fecha_registro" not in cols:
            c.execute("ALTER TABLE plan ADD COLUMN fecha_registro TEXT")
        pcols = [r[1] for r in c.execute("PRAGMA table_info(patients)").fetchall()]
        if "alergias" not in pcols:
            c.execute("ALTER TABLE patients ADD COLUMN alergias TEXT")
        if "antecedentes" not in pcols:
            c.execute("ALTER TABLE patients ADD COLUMN antecedentes TEXT")
        if "foto" not in pcols:
            c.execute("ALTER TABLE patients ADD COLUMN foto TEXT")
        ccols = [r[1] for r in c.execute("PRAGMA table_info(citas)").fetchall()]
        if "dentist_id" not in ccols:
            c.execute("ALTER TABLE citas ADD COLUMN dentist_id INTEGER")
        if not c.execute("SELECT 1 FROM users WHERE usuario='admin'").fetchone():
            c.execute("INSERT INTO users(usuario,nombre,rol,clave) VALUES('admin','Administrador','admin',?)",
                      (generate_password_hash(os.environ.get("ADMIN_PASSWORD", "admin123")),))

_FAILS = {}  # usuario -> (intentos, bloqueado_hasta_ts)

def audit(accion, detalle=""):
    try:
        ex("INSERT INTO auditoria(fecha,usuario,accion,detalle,ip) VALUES(?,?,?,?,?)",
           (dt.datetime.now().isoformat(timespec="seconds"), session.get("u", "anonimo"), accion, detalle, request.remote_addr))
    except Exception:
        pass

def clave_segura(p):
    return len(p) >= 8 and re.search(r"[A-Za-z]", p) and re.search(r"[0-9]", p)

def backup_db():
    os.makedirs(os.path.join(BASE, "backups"), exist_ok=True)
    dest = os.path.join(BASE, "backups", f"consultorio_{dt.date.today().isoformat()}.db")
    if not os.path.exists(dest):
        shutil.copy2(DB, dest)

def need(*roles):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            if "u" not in session: return redirect("/login")
            if roles and session["rol"] not in roles and session["rol"] != "admin": abort(403)
            return f(*a, **k)
        return w
    return deco

def wa(tel, msg):
    d = "".join(ch for ch in (tel or "") if ch.isdigit())
    if len(d) == 10: d = "57" + d
    return f"https://wa.me/{d}?text={up.quote(msg)}" if d else ""
app.jinja_env.globals["wa"] = wa

LAYOUT = """<!doctype html><html lang=es><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{{t}} - Consultorio</title><style>
:root{--ink:#1e293b;--azul:#2563eb;--azul-osc:#1e3a8a;--azul-suave:#eef4ff;--paper:#f4f7fb;--line:#e2e8f0;
--acento:#14b8a6;--mal:#e11d48;--bien:#16a34a;--adv:#f59e0b;--mute:#64748b;--card:#ffffff}
*{box-sizing:border-box}
body{margin:0;font:16px/1.55 "Segoe UI",system-ui,sans-serif;color:var(--ink);background:var(--paper);display:flex;min-height:100vh}
nav{width:240px;background:linear-gradient(180deg,#1e3a8a,#172e68);color:#fff;padding:22px 14px;flex:none;box-shadow:2px 0 10px rgba(0,0,0,.08);position:sticky;top:0;align-self:flex-start;height:100vh;overflow-y:auto;scrollbar-width:thin}
nav b{display:block;font-size:17px;margin-bottom:4px;color:#fff;letter-spacing:.02em}
nav small{display:block;color:#aecbf5;margin-bottom:20px;font-size:11px}
nav .grp{color:#8fb8da;font-size:11px;text-transform:uppercase;letter-spacing:.07em;margin:16px 6px 6px;font-weight:600}
nav a{display:block;color:#eaf2fa;text-decoration:none;padding:9px 12px;border-radius:8px;font-size:14px;margin-bottom:2px;transition:background .15s}
nav a:hover{background:rgba(255,255,255,.14)}
main{flex:1;padding:30px 34px;max-width:1100px;overflow-x:auto;position:relative}
.marca-agua{position:fixed;right:16px;bottom:16px;z-index:40;pointer-events:none}
.marca-agua img{width:96px;height:96px;max-width:96px;max-height:96px;opacity:1;border-radius:50%;object-fit:cover;box-shadow:0 3px 14px rgba(0,0,0,.3);border:3px solid #fff}

main>*{position:relative;z-index:1}

h1{margin:0 0 18px;font-size:27px;color:var(--azul-osc);font-weight:700}
h2{font-size:18px;margin:30px 0 10px;color:var(--azul-osc);font-weight:600;border-bottom:2px solid var(--azul-suave);padding-bottom:6px}
table{border-collapse:separate;border-spacing:0;width:100%;background:var(--card);border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(15,23,42,.08)}
td,th{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;font-size:14.5px}
th{background:var(--azul-suave);color:var(--azul-osc);font-weight:600;text-transform:uppercase;font-size:12px;letter-spacing:.03em}
tr:last-child td{border-bottom:none}
input,select,textarea{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:#fff;max-width:100%}
input:focus,select:focus,textarea:focus{border-color:var(--azul);outline:none;box-shadow:0 0 0 3px rgba(37,99,235,.15)}
button,.btn{font:inherit;font-weight:600;background:var(--azul);color:#fff;border:0;padding:9px 16px;border-radius:8px;cursor:pointer;
  text-decoration:none;display:inline-block;box-shadow:0 1px 2px rgba(37,99,235,.3);transition:background .15s,transform .1s}
button:hover,.btn:hover{background:var(--azul-osc)}
button:active,.btn:active{transform:translateY(1px)}
button:focus-visible,a:focus-visible,input:focus-visible{outline:3px solid var(--acento)}
form.row{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0;align-items:center}
.msg{background:#fff7e0;border-left:4px solid var(--adv);padding:10px 14px;margin-bottom:14px;border-radius:8px}
.bar{height:11px;background:var(--line);border-radius:6px;overflow:hidden}
.bar i{display:block;height:11px;background:linear-gradient(90deg,var(--acento),#0d9488);border-radius:6px}
.card{background:var(--card);border-radius:14px;padding:18px 20px;box-shadow:0 1px 4px rgba(15,23,42,.08);margin:14px 0;transition:box-shadow .2s,transform .2s}
.card:hover{box-shadow:0 4px 14px rgba(15,23,42,.12)}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:20px}
.brand-badge{width:42px;height:42px;border-radius:50%;background:linear-gradient(135deg,#60a5fa,var(--acento));color:#fff;
  display:flex;align-items:center;justify-content:center;font-family:Georgia,"Times New Roman",serif;font-weight:700;font-size:16px;flex:none;box-shadow:0 2px 6px rgba(0,0,0,.25)}
.brand-logo{width:42px;height:42px;border-radius:50%;object-fit:cover;background:#fff;flex:none;box-shadow:0 2px 6px rgba(0,0,0,.25)}
nav .brand b{font-family:Georgia,"Times New Roman",serif;font-size:18px;letter-spacing:.01em}
.stats{display:flex;gap:16px;flex-wrap:wrap;margin:14px 0 22px}
.stat{flex:1;min-width:160px;border-radius:16px;padding:18px 20px;box-shadow:0 4px 14px rgba(37,99,235,.12);
  color:#fff;background:linear-gradient(135deg,#3b82f6,#1e3a8a);position:relative;overflow:hidden;transition:transform .2s,box-shadow .2s}
.stat:hover{transform:translateY(-3px);box-shadow:0 8px 20px rgba(37,99,235,.22)}
.stat.v2{background:linear-gradient(135deg,#2dd4bf,#0d9488)}
.stat.v3{background:linear-gradient(135deg,#fbbf24,#d97706)}
.stat.v4{background:linear-gradient(135deg,#a78bfa,#7c3aed)}
.stat b{display:block;font-size:30px;color:#fff;line-height:1.1;text-shadow:0 1px 2px rgba(0,0,0,.15)}
.stat span{display:block;font-size:12px;color:rgba(255,255,255,.92);margin-top:5px;text-transform:uppercase;letter-spacing:.04em;font-weight:600}
.citas-hoy{display:flex;flex-direction:column;gap:10px}
.cita-card{display:flex;align-items:center;gap:14px;background:var(--card);border-radius:14px;padding:13px 18px;
  box-shadow:0 2px 8px rgba(15,23,42,.08);border-left:5px solid var(--acento);transition:transform .15s,box-shadow .15s}
.cita-card:hover{transform:translateX(3px);box-shadow:0 4px 14px rgba(15,23,42,.14)}
.cita-card .hora{font-weight:700;color:#fff;min-width:60px;font-size:14px;text-align:center;background:linear-gradient(135deg,#3b82f6,#1e3a8a);
  border-radius:8px;padding:6px 4px}
.cita-card .info{flex:1}
.cita-card .info b{display:block;font-size:14.5px;color:var(--azul-osc)}
.cita-card .info small{color:var(--mute)}
.pill{display:inline-block;padding:3px 10px;border-radius:99px;font-size:11.5px;font-weight:600;text-transform:uppercase;letter-spacing:.02em}
.pill.pendiente{background:#fff3cd;color:#92680a}.pill.confirmada{background:#dcfce7;color:#15803d}
.pill.completada{background:#dbeafe;color:#1d4ed8}.pill.cancelada{background:#fee2e2;color:#b91c1c}
@media(max-width:700px){
body{display:block}
nav{width:auto;height:auto;max-height:56px;overflow:hidden;position:relative;padding:10px 14px;transition:max-height .25s ease}
nav.abierto{max-height:2000px;overflow-y:auto}
nav b{display:inline-block;margin:0 8px 0 0}
nav small{display:none}
nav .toggle-menu{display:block;width:100%;text-align:left;background:rgba(255,255,255,.12);color:#fff;border:none;padding:8px 12px;border-radius:8px;font-size:14px;font-weight:600;margin-bottom:6px}
main{padding:16px}
}
@media(min-width:701px){.toggle-menu{display:none}}
@media print{nav,.noprint{display:none}}
</style></head><body>
{% if session.u %}<nav id=navbar><button type=button class=toggle-menu onclick="document.getElementById('navbar').classList.toggle('abierto')">☰ Menú</button><div class=brand>{% if logo_file %}<img src="/logo?v={{logo_file}}" class=brand-logo>{% else %}<span class=brand-badge>NM</span>{% endif %}<div><b>Dra. Natalia Morera</b><small>Odontología Especializada</small></div></div>
<div style="position:relative;margin:4px 0 10px">
<button type=button onclick="var d=document.getElementById('avisos-drop');d.style.display=d.style.display=='block'?'none':'block'"
style="width:100%;text-align:left;background:rgba(255,255,255,.12);color:#fff;border:none;padding:8px 10px;border-radius:8px;font-size:13px;font-weight:600">
🔔 Avisos {% if avisos %}<span style="background:#ef4444;color:#fff;border-radius:10px;padding:1px 7px;font-size:11px;margin-left:4px">{{avisos|length}}</span>{% endif %}
</button>
<div id=avisos-drop style="display:none;position:absolute;top:100%;left:0;right:0;background:#fff;color:#1e293b;border-radius:8px;margin-top:4px;padding:8px;z-index:50;box-shadow:0 6px 18px rgba(0,0,0,.25);font-size:13px">
{% if avisos %}{% for a in avisos %}<div style="padding:5px 4px;border-bottom:1px solid #eef2f7">{{a}}</div>{% endfor %}
{% else %}<div style="padding:5px 4px;color:#94a3b8">Sin avisos pendientes.</div>{% endif %}
</div></div>
<form action=/buscar method=get style="margin:4px 0 14px"><input name=q placeholder="🔎 Buscar paciente, cita..." style="width:100%;padding:8px 10px;border-radius:8px;border:none;font-size:13px"></form>
<div class=grp>Panel</div><a href=/>Inicio</a>
<div class=grp>Pacientes</div><a href=/pacientes>Pacientes</a><a href=/citas>Citas / Agenda</a><a href=/recordatorios>Recordatorios</a><a href=/reactivacion>Reactivar pacientes</a><a href=/cumpleanos>🎂 Cumpleaños del mes</a>
<div class=grp>Catálogos</div><a href=/odontologos>Odontólogos</a><a href=/servicios>Servicios</a>
<div class=grp>Punto de venta</div><a href=/ventas>Punto de venta</a><a href=/productos>Inventario</a><a href=/caja>Caja</a><a href=/gastos>Gastos</a>
<div class=grp>Reportes y cumplimiento</div><a href=/estadisticas>Estadísticas</a><a href=/resumen-mensual>Resumen mensual</a><a href=/rips>Reporte RIPS</a>
<div class=grp>Facturación</div><a href=/ventas>Facturación electrónica</a>
{% if session.rol=='admin' %}<div class=grp>Administración</div><a href=/usuarios>Usuarios y personal</a><a href=/configuracion>Ajustes</a>{% endif %}
<div class=grp>Portal</div><a href=/reservar>Portal de reservas</a>
<a href=/logout style="margin-top:14px;color:#ffd3d3">Cerrar sesión ({{session.u}})</a></nav>{% endif %}

<script>
document.addEventListener("keydown", function(e){
  var tag = (document.activeElement && document.activeElement.tagName) || "";
  var enCampo = ["INPUT","TEXTAREA","SELECT"].includes(tag);
  if (!enCampo) {
    if (e.key === "ArrowDown") { window.scrollBy({top: 220, behavior: "smooth"}); e.preventDefault(); }
    if (e.key === "ArrowUp")   { window.scrollBy({top: -220, behavior: "smooth"}); e.preventDefault(); }
  }
  if ((e.key === "Delete" || e.key === "Backspace") && !enCampo) {
    var sel = document.querySelector("tr.fila-sel");
    if (sel) {
      var form = sel.querySelector("form[action$=\'/eliminar\']");
      if (form && confirm("¿Eliminar este registro seleccionado?")) { form.requestSubmit ? form.requestSubmit() : form.submit(); }
    }
  }
});
document.addEventListener("click", function(e){
  var fila = e.target.closest("tr");
  if (!fila || !fila.closest("table")) return;
  if (e.target.closest("form,button,a,select,input")) return;
  document.querySelectorAll("tr.fila-sel").forEach(f=>f.classList.remove("fila-sel"));
  fila.classList.add("fila-sel");
});
</script>
<style>tr.fila-sel{outline:2px solid #2563eb;outline-offset:-2px;background:#eff6ff !important}</style>
<main><div class=marca-agua><img src="/marca"></div>{% for m in get_flashed_messages() %}<div class=msg>{{m}}</div>{% endfor %}%%BODY%%</main></body></html>"""

def notificaciones():
    avisos = []
    try:
        maniana = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        sin_conf = q("SELECT COUNT(*) n FROM citas WHERE fecha LIKE ? AND estado IN('pendiente','confirmada')", (maniana + "%",), one=True)["n"]
        if sin_conf:
            avisos.append(f"{sin_conf} cita(s) agendada(s) para mañana")
    except Exception:
        pass
    try:
        pend = q("SELECT COUNT(*) n FROM cuentas WHERE estado!='pagada' AND COALESCE(abonado,0) < COALESCE(total,0)", one=True)["n"]
        if pend:
            avisos.append(f"{pend} cuenta(s) de paciente con saldo pendiente")
    except Exception:
        pass
    try:
        bajo = q("SELECT COUNT(*) n FROM products WHERE active=1 AND stock IS NOT NULL AND min IS NOT NULL AND stock<=min", one=True)["n"]
        if bajo:
            avisos.append(f"{bajo} producto(s) con stock bajo")
    except Exception:
        pass
    try:
        maniana2 = (dt.date.today() + dt.timedelta(days=0)).strftime("%m-%d")
        cumple = q("SELECT COUNT(*) n FROM patients WHERE fecha_nac IS NOT NULL AND substr(fecha_nac,6,5)=?", (maniana2,), one=True)["n"]
        if cumple:
            avisos.append(f"{cumple} paciente(s) cumple(n) años hoy")
    except Exception:
        pass
    return avisos

def page(t, body, **c):
    logo_file = ""
    avisos = []
    if session.get("u"):
        row = q("SELECT value FROM settings WHERE key='logo_file'", one=True)
        logo_file = row["value"] if row else ""
        avisos = notificaciones()
    return render_template_string(LAYOUT.replace("%%BODY%%", body), t=t, logo_file=logo_file, avisos=avisos, **c)

LOGIN_FORM = """<h1>Entrar</h1><form method=post class=row><input name=usuario placeholder=Usuario required autofocus>
<input name=clave type=password placeholder=Contraseña required><button>Entrar</button></form>
<p>¿Eres paciente? <a href=/reservar>Reserva tu cita aquí</a>.</p>"""

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        usuario = request.form["usuario"].strip()
        intentos, bloqueado_hasta = _FAILS.get(usuario, (0, 0))
        if time.time() < bloqueado_hasta:
            flash("Demasiados intentos. Intenta de nuevo en unos minutos.")
            return page("Entrar", LOGIN_FORM)
        u = q("SELECT * FROM users WHERE usuario=? AND activo=1", (usuario,), one=True)
        if u and check_password_hash(u["clave"], request.form["clave"]):
            _FAILS.pop(usuario, None)
            session.update(u=u["usuario"], rol=u["rol"])
            audit("login", "inicio de sesion")
            return redirect("/")
        intentos += 1
        _FAILS[usuario] = (intentos, time.time() + 300 if intentos >= 5 else 0)
        flash("Usuario o contraseña incorrectos.")
    return page("Entrar", LOGIN_FORM)

@app.route("/logout")
def logout():
    session.clear(); return redirect("/login")

@app.route("/")
@need()
def home():
    hoy = dt.date.today().isoformat()
    citas = q("SELECT c.*,p.nombre FROM citas c LEFT JOIN patients p ON p.id=c.patient_id WHERE fecha LIKE ? ORDER BY fecha", (hoy + "%",))
    n = q("SELECT COUNT(*) n FROM citas WHERE estado='pendiente' AND origen='portal'", one=True)["n"]
    total_pac = q("SELECT COUNT(*) n FROM patients", one=True)["n"]
    mes = hoy[:7]
    pac_mes = q("SELECT COUNT(*) n FROM patients WHERE creado LIKE ?", (mes + "%",), one=True)["n"]
    completadas_hoy = q("SELECT COUNT(*) n FROM citas WHERE fecha LIKE ? AND estado='completada'", (hoy + "%",), one=True)["n"]
    return page("Inicio", """<h1 style="background:linear-gradient(90deg,#1e3a8a,#0ea5e9,#14b8a6);-webkit-background-clip:text;background-clip:text;color:transparent">&#128197; Hoy, {{hoy_legible}}</h1>
{% if n %}<div class=msg>{{n}} reserva(s) del portal esperan confirmación. <a href=/citas>Ver citas</a></div>{% endif %}
<div class=stats>
<div class=stat><b>{{citas|length}}</b><span>Citas hoy</span></div>
<div class="stat v2"><b>{{completadas_hoy}}</b><span>Completadas hoy</span></div>
<div class="stat v3"><b>{{pac_mes}}</b><span>Pacientes nuevos del mes</span></div>
<div class="stat v4"><b>{{total_pac}}</b><span>Pacientes totales</span></div>
</div>
<h2>Agenda de hoy</h2>
<div class=citas-hoy>{% for c in citas %}<div class=cita-card><div class=hora>{{c.fecha[11:]}}</div>
<div class=info><b>{{c.nombre or c.nombre_libre}}</b><small>{{c.motivo or 'Sin motivo registrado'}}</small></div>
<span class="pill {{c.estado}}">{{c.estado}}</span></div>
{% else %}<p>No hay citas para hoy.</p>{% endfor %}</div>""",
        citas=citas, n=n, total_pac=total_pac, pac_mes=pac_mes, completadas_hoy=completadas_hoy,
        hoy_legible=dt.date.today().strftime("%d/%m/%Y"))

@app.route("/estadisticas")
@need("odontologo", "admin")
def estadisticas():
    hoy = dt.date.today()
    meses = []
    for i in range(5, -1, -1):
        m = (hoy.replace(day=1) - dt.timedelta(days=1)) if i == 0 else hoy
        y, mo = hoy.year, hoy.month - i
        while mo <= 0:
            mo += 12; y -= 1
        meses.append(f"{y:04d}-{mo:02d}")
    ingresos_mes = []
    for ym in meses:
        v1 = q("SELECT COALESCE(SUM(valor),0) t FROM atenciones WHERE fecha LIKE ?", (ym + "%",), one=True)["t"] or 0
        v2 = q("SELECT COALESCE(SUM(monto),0) t FROM abonos WHERE fecha LIKE ?", (ym + "%",), one=True)["t"] or 0
        ingresos_mes.append(float(v1) + float(v2))
    max_ing = max(ingresos_mes) if max(ingresos_mes, default=0) > 0 else 1
    barras_ingresos = ""
    bw, gap, base_y, max_h = 54, 18, 230, 170
    for idx, (ym, val) in enumerate(zip(meses, ingresos_mes)):
        h = (val / max_ing) * max_h if max_ing else 0
        x = 20 + idx * (bw + gap)
        y = base_y - h
        mes_lbl = ["Ene","Feb","Mar","Abr","May","Jun","Jul","Ago","Sep","Oct","Nov","Dic"][int(ym[5:7]) - 1]
        barras_ingresos += (
            f'<rect x="{x}" y="{y}" width="{bw}" height="{h}" rx="6" fill="url(#gradIngresos)"/>'
            f'<text x="{x+bw/2}" y="{base_y+18}" text-anchor="middle" font-size="12" fill="#64748b" font-family="Arial">{mes_lbl}</text>'
            f'<text x="{x+bw/2}" y="{y-6}" text-anchor="middle" font-size="11" fill="#1e3a8a" font-family="Arial" font-weight="600">${val:,.0f}</text>'
        )
    svg_ingresos = (
        '<svg viewBox="0 0 460 260" style="width:100%;max-width:520px;height:auto">'
        '<defs><linearGradient id="gradIngresos" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0%" stop-color="#2563eb"/><stop offset="100%" stop-color="#14b8a6"/></linearGradient></defs>'
        f'<line x1="15" y1="{base_y}" x2="445" y2="{base_y}" stroke="#e2e8f0" stroke-width="1.5"/>'
        + barras_ingresos + '</svg>'
    )

    estados = ["pendiente", "confirmada", "completada", "cancelada"]
    colores_estado = {"pendiente": "#f59e0b", "confirmada": "#2563eb", "completada": "#16a34a", "cancelada": "#e11d48"}
    conteos = {e: q("SELECT COUNT(*) n FROM citas WHERE estado=?", (e,), one=True)["n"] for e in estados}
    total_citas = sum(conteos.values()) or 1
    cx_pie, cy_pie, r_pie = 110, 110, 95
    ang = -90
    slices = ""
    for e in estados:
        frac = conteos[e] / total_citas
        if frac <= 0:
            continue
        ang2 = ang + frac * 360
        import math as _m
        x1 = cx_pie + r_pie * _m.cos(_m.radians(ang)); y1 = cy_pie + r_pie * _m.sin(_m.radians(ang))
        x2 = cx_pie + r_pie * _m.cos(_m.radians(ang2)); y2 = cy_pie + r_pie * _m.sin(_m.radians(ang2))
        large = 1 if (ang2 - ang) > 180 else 0
        slices += f'<path d="M{cx_pie},{cy_pie} L{x1:.1f},{y1:.1f} A{r_pie},{r_pie} 0 {large} 1 {x2:.1f},{y2:.1f} Z" fill="{colores_estado[e]}" stroke="#fff" stroke-width="2"/>'
        ang = ang2
    leyenda_citas = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:6px;margin:4px 12px 4px 0;font-size:13px;color:var(--mute)">'
        f'<span style="width:12px;height:12px;background:{colores_estado[e]};border-radius:3px;display:inline-block"></span>{e.capitalize()} ({conteos[e]})</span>'
        for e in estados
    )
    svg_pie = f'<svg viewBox="0 0 220 220" style="width:100%;max-width:260px;height:auto">{slices}</svg>'

    top_trat = q("""SELECT tratamiento, COUNT(*) n FROM plan GROUP BY tratamiento ORDER BY n DESC LIMIT 6""")
    max_trat = max((t["n"] for t in top_trat), default=1) or 1
    filas_trat = ""
    for t in top_trat:
        pct = (t["n"] / max_trat) * 100
        filas_trat += (
            f'<div style="margin-bottom:10px"><div style="display:flex;justify-content:space-between;font-size:13.5px;margin-bottom:3px">'
            f'<span>{t["tratamiento"]}</span><b>{t["n"]}</b></div>'
            f'<div style="height:10px;background:var(--line);border-radius:5px;overflow:hidden">'
            f'<div style="height:10px;width:{pct:.0f}%;background:linear-gradient(90deg,#2563eb,#14b8a6);border-radius:5px"></div></div></div>'
        )

    return page("Estadísticas", """<h1>Estadísticas del consultorio</h1>
<div class=card>
<h2 style="margin-top:0;border:0">Ingresos de los últimos 6 meses</h2>
""" + svg_ingresos + """
</div>
<div class=card style="display:flex;gap:28px;flex-wrap:wrap;align-items:center">
<div><h2 style="margin-top:0;border:0">Citas por estado</h2>""" + svg_pie + """</div>
<div>""" + leyenda_citas + """</div>
</div>
<div class=card>
<h2 style="margin-top:0;border:0">Tratamientos más realizados</h2>
""" + (filas_trat or "<p>Todavía no hay suficientes datos.</p>") + """
</div>""")


@app.route("/buscar")
@need("odontologo", "recepcion", "radiologo")
def buscar():
    term = request.args.get("q", "").strip()
    pac_r, cit_r, ven_r = [], [], []
    if term:
        like = f"%{term}%"
        pac_r = q("SELECT id,nombre,documento,telefono FROM patients WHERE nombre LIKE ? OR documento LIKE ? OR telefono LIKE ? ORDER BY nombre LIMIT 20",
                   (like, like, like))
        cit_r = q("""SELECT c.id,c.fecha,c.motivo,c.estado,COALESCE(p.nombre,c.nombre_libre) nombre FROM citas c
                     LEFT JOIN patients p ON p.id=c.patient_id
                     WHERE COALESCE(p.nombre,c.nombre_libre) LIKE ? OR c.motivo LIKE ?
                     ORDER BY c.fecha DESC LIMIT 20""", (like, like))
        ven_r = q("""SELECT s.id,s.date,s.total,COALESCE(p.nombre,'Sin paciente') nombre FROM sales s
                     LEFT JOIN patients p ON p.id=s.patient_id
                     WHERE COALESCE(p.nombre,'') LIKE ? ORDER BY s.date DESC LIMIT 20""", (like,))
    return page("Buscar", """
<h1>Buscar en todo el sistema</h1>
<form class=row style="margin-bottom:18px"><input name=q value="{{term}}" placeholder="Nombre, documento, teléfono o motivo..." style="min-width:300px" autofocus><button>Buscar</button></form>
{% if term %}
<h2>Pacientes ({{pac_r|length}})</h2>
{% if pac_r %}<table class=tbl><tr><th>Nombre<th>Documento<th>Teléfono<th></tr>
{% for p in pac_r %}<tr><td>{{p.nombre}}<td>{{p.documento or '—'}}<td>{{p.telefono or '—'}}<td><a href="/pacientes/{{p.id}}">Ver ficha</a></tr>{% endfor %}</table>
{% else %}<p style="color:var(--mute)">Sin resultados.</p>{% endif %}

<h2>Citas ({{cit_r|length}})</h2>
{% if cit_r %}<table class=tbl><tr><th>Fecha<th>Paciente<th>Motivo<th>Estado</tr>
{% for cc in cit_r %}<tr><td>{{cc.fecha}}<td>{{cc.nombre}}<td>{{cc.motivo or '—'}}<td>{{cc.estado}}</tr>{% endfor %}</table>
{% else %}<p style="color:var(--mute)">Sin resultados.</p>{% endif %}

<h2>Ventas ({{ven_r|length}})</h2>
{% if ven_r %}<table class=tbl><tr><th>Fecha<th>Paciente<th>Total</tr>
{% for v in ven_r %}<tr><td>{{v.date}}<td>{{v.nombre}}<td>${{"%.0f"|format(v.total or 0)}}</tr>{% endfor %}</table>
{% else %}<p style="color:var(--mute)">Sin resultados.</p>{% endif %}
{% endif %}
""", term=term, pac_r=pac_r, cit_r=cit_r, ven_r=ven_r)

@app.route("/resumen-mensual")
@need()
def resumen_mensual():
    hoy = dt.date.today()
    try:
        anio = int(request.args.get("anio", hoy.year))
        mes = int(request.args.get("mes", hoy.month))
    except ValueError:
        anio, mes = hoy.year, hoy.month
    mes_ant = (anio, mes - 1) if mes > 1 else (anio - 1, 12)
    mes_sig = (anio, mes + 1) if mes < 12 else (anio + 1, 1)
    nombres_mes = ["", "Enero","Febrero","Marzo","Abril","Mayo","Junio","Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"]
    ym = f"{anio:04d}-{mes:02d}"

    ing_atenciones = float(q("SELECT COALESCE(SUM(valor),0) t FROM atenciones WHERE fecha LIKE ?", (ym + "%",), one=True)["t"] or 0)
    ing_abonos = float(q("SELECT COALESCE(SUM(monto),0) t FROM abonos WHERE fecha LIKE ?", (ym + "%",), one=True)["t"] or 0)
    ing_ventas = float(q("SELECT COALESCE(SUM(total),0) t FROM sales WHERE date LIKE ?", (ym + "%",), one=True)["t"] or 0)
    total_ingresos = ing_atenciones + ing_abonos + ing_ventas
    total_gastos = float(q("SELECT COALESCE(SUM(amount),0) t FROM expenses WHERE date LIKE ?", (ym + "%",), one=True)["t"] or 0)
    balance = total_ingresos - total_gastos

    citas_total = q("SELECT COUNT(*) n FROM citas WHERE fecha LIKE ?", (ym + "%",), one=True)["n"]
    citas_atendidas = q("SELECT COUNT(*) n FROM citas WHERE fecha LIKE ? AND estado='atendida'", (ym + "%",), one=True)["n"]
    pac_nuevos = q("SELECT COUNT(*) n FROM patients WHERE creado LIKE ?", (ym + "%",), one=True)["n"]
    pac_distintos = q("SELECT COUNT(DISTINCT patient_id) n FROM atenciones WHERE fecha LIKE ? AND patient_id IS NOT NULL", (ym + "%",), one=True)["n"]

    return page("Resumen mensual", """
<div class=noprint><h1>Resumen de {{nombres_mes[mes]}} {{anio}}</h1>
<div class=row style="margin-bottom:14px">
<a href="/resumen-mensual?anio={{mes_ant[0]}}&mes={{mes_ant[1]}}">&larr; Mes anterior</a>
<a href="/resumen-mensual?anio={{mes_sig[0]}}&mes={{mes_sig[1]}}">Mes siguiente &rarr;</a>
<button onclick="window.print()">Imprimir / Guardar como PDF</button>
</div></div>
<h1 style="display:none" class=onlyprint>Resumen de {{nombres_mes[mes]}} {{anio}} — Dra. Natalia Morera Odontología Especializada</h1>
<style>@media print{.onlyprint{display:block!important}}</style>
<div class=stats>
<div class=stat style="background:linear-gradient(135deg,#2563eb,#1d4ed8)"><b>${{"%.0f"|format(total_ingresos)}}</b><span>Ingresos del mes</span></div>
<div class=stat style="background:linear-gradient(135deg,#dc2626,#b91c1c)"><b>${{"%.0f"|format(total_gastos)}}</b><span>Gastos del mes</span></div>
<div class=stat style="background:linear-gradient(135deg,#16a34a,#15803d)"><b>${{"%.0f"|format(balance)}}</b><span>Balance del mes</span></div>
<div class=stat style="background:linear-gradient(135deg,#7c3aed,#6d28d9)"><b>{{pac_nuevos}}</b><span>Pacientes nuevos</span></div>
</div>
<h2>Detalle de ingresos</h2>
<table class=tbl><tr><th>Concepto<th>Monto</tr>
<tr><td>Atenciones clínicas<td>${{"%.0f"|format(ing_atenciones)}}</tr>
<tr><td>Abonos de pacientes<td>${{"%.0f"|format(ing_abonos)}}</tr>
<tr><td>Ventas (productos)<td>${{"%.0f"|format(ing_ventas)}}</tr>
</table>
<h2>Citas y pacientes</h2>
<table class=tbl><tr><th>Indicador<th>Valor</tr>
<tr><td>Citas agendadas en el mes<td>{{citas_total}}</tr>
<tr><td>Citas atendidas<td>{{citas_atendidas}}</tr>
<tr><td>Pacientes distintos atendidos<td>{{pac_distintos}}</tr>
<tr><td>Pacientes nuevos registrados<td>{{pac_nuevos}}</tr>
</table>
""", nombres_mes=nombres_mes, anio=anio, mes=mes, mes_ant=mes_ant, mes_sig=mes_sig,
        total_ingresos=total_ingresos, total_gastos=total_gastos, balance=balance,
        ing_atenciones=ing_atenciones, ing_abonos=ing_abonos, ing_ventas=ing_ventas,
        citas_total=citas_total, citas_atendidas=citas_atendidas, pac_nuevos=pac_nuevos, pac_distintos=pac_distintos)

@app.route("/cumpleanos")
@need("odontologo", "recepcion")
def cumpleanos():
    hoy = dt.date.today()
    mes_str = f"{hoy.month:02d}"
    pacientes_mes = q("SELECT * FROM patients WHERE fecha_nac IS NOT NULL AND substr(fecha_nac,6,2)=?", (mes_str,))
    pacientes_mes = sorted(pacientes_mes, key=lambda p: p["fecha_nac"][8:10])
    nombres_mes = ["", "Enero","Febrero","Marzo","Abril","Mayo","Junio","Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"]
    return page("Cumpleaños del mes", """<h1>🎂 Cumpleaños de """ + nombres_mes[hoy.month] + """</h1>
{% if pacientes_mes %}<table class=tbl><tr><th>Día<th>Paciente<th>Teléfono<th>Correo</tr>
{% for p in pacientes_mes %}<tr{% if p.fecha_nac[8:10]|int==hoy.day %} style="background:#fef3c7;font-weight:600"{% endif %}>
<td>{{p.fecha_nac[8:10]}}<td>{{p.nombre}}{% if p.fecha_nac[8:10]|int==hoy.day %} 🎉 ¡Hoy!{% endif %}<td>{{p.telefono or '—'}}<td>{{p.email or '—'}}</tr>
{% endfor %}</table>
{% else %}<p style="color:var(--mute)">No hay pacientes con cumpleaños registrado este mes.</p>{% endif %}""",
        pacientes_mes=pacientes_mes, hoy=hoy)

@app.route("/pacientes", methods=["GET", "POST"])
@need("odontologo", "recepcion", "radiologo")
def pacientes():
    if request.method == "POST":
        f = request.form
        if not f["nombre"].strip(): flash("El nombre es obligatorio.")
        else:
            pid = ex("INSERT INTO patients(nombre,documento,tipo_doc,fecha_nac,sexo,telefono,email,creado) VALUES(?,?,?,?,?,?,?,?)",
                     (f["nombre"].strip(), f["documento"], f.get("tipo_doc","CC"), f.get("fecha_nac") or None, f.get("sexo","F"), f["telefono"], f["email"], dt.date.today().isoformat()))
            foto = request.files.get("foto")
            if foto and foto.filename:
                ext = os.path.splitext(foto.filename)[1].lower()
                tipos_foto = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
                if ext in tipos_foto:
                    nombre_foto = f"foto_paciente_{pid}{ext}"
                    if storage_guardar(nombre_foto, foto.read(), tipos_foto[ext]):
                        ex("UPDATE patients SET foto=? WHERE id=?", (nombre_foto, pid))
            audit("crear_paciente", f["nombre"].strip())
            return redirect(f"/pacientes/{pid}")
    s = "%" + request.args.get("s", "") + "%"
    ps = q("SELECT * FROM patients WHERE nombre LIKE ? OR documento LIKE ? ORDER BY nombre", (s, s))
    return page("Pacientes", """<h1>Pacientes</h1><form class=row><input name=s placeholder="Buscar nombre o documento" value="{{request.args.s}}"><button>Buscar</button></form>
<table><tr><th>Nombre<th>Documento<th>Teléfono</tr>{% for p in ps %}<tr><td><a href=/pacientes/{{p.id}}>{{p.nombre}}</a><td>{{p.documento}}<td>{{p.telefono}}</tr>{% endfor %}</table>
<h2>Nuevo paciente</h2><form method=post class=row enctype="multipart/form-data"><input name=nombre placeholder=Nombre required>
<input type=file name=foto accept="image/png,image/jpeg,image/webp" title="Fotografía del paciente (opcional)">
<select name=tipo_doc><option value=CC>Cédula</option><option value=TI>Tarjeta de identidad</option><option value=RC>Registro civil</option><option value=CE>Cédula extranjería</option><option value=PA>Pasaporte</option></select>
<input name=documento placeholder=Documento><input name=fecha_nac type=date title="Fecha de nacimiento">
<select name=sexo><option value=F>Femenino</option><option value=M>Masculino</option></select>
<input name=telefono placeholder=Teléfono><input name=email placeholder=Correo type=email><button>Guardar paciente</button></form>""", ps=ps)

@app.route("/pacientes/<int:pid>")
@need("odontologo", "recepcion", "radiologo")
def paciente(pid):
    p = q("SELECT * FROM patients WHERE id=?", (pid,), one=True) or abort(404)
    plan = q("SELECT * FROM plan WHERE patient_id=? ORDER BY etapa,orden,id", (pid,))
    tot = sum(x["costo"] or 0 for x in plan); hecho = sum(x["costo"] or 0 for x in plan if x["estado"] == "hecho")
    plan_g = []
    for x in plan:
        if plan_g and plan_g[-1][0] == (x["etapa"] or "Sin etapa"):
            plan_g[-1][1].append(x)
        else:
            plan_g.append([x["etapa"] or "Sin etapa", [x]])
    proxima_cita = q("""SELECT * FROM citas WHERE patient_id=? AND fecha>=? AND estado IN('pendiente','confirmada')
                        ORDER BY fecha LIMIT 1""", (pid, dt.datetime.now().strftime("%Y-%m-%d %H:%M")), one=True)
    return page(p["nombre"], """<div class=row style="justify-content:space-between;align-items:flex-start">
<div class=row style="align-items:flex-start">
{% if p.foto %}<img src="/foto/{{p.id}}" style="width:84px;height:84px;object-fit:cover;border-radius:10px;border:1px solid #e2e8f0">
{% else %}<div style="width:84px;height:84px;border-radius:10px;background:#eef2f7;display:flex;align-items:center;justify-content:center;color:var(--mute);font-size:12px;text-align:center">Sin foto</div>{% endif %}
<div><h1 style="margin-bottom:4px">{{p.nombre}}</h1><p>{{p.documento}} · {{p.telefono}} · {{p.email}}</p>
{% if session.rol in ['admin','recepcion','odontologo'] %}<form method=post action="/pacientes/{{p.id}}/foto" enctype="multipart/form-data" class=row style="margin-top:4px">
<input type=file name=foto accept="image/png,image/jpeg,image/webp" style="font-size:12px"><button style="padding:4px 10px;font-size:12px">{{'Cambiar foto' if p.foto else 'Subir foto'}}</button></form>{% endif %}</div>
</div>
{% if session.rol in ['admin','recepcion'] %}<form method=post action="/pacientes/{{p.id}}/eliminar" onsubmit="return confirm('¿Eliminar al paciente {{p.nombre}}? Se borrará toda su información: historia, plan, odontograma, citas, pagos y radiografías. Esta acción no se puede deshacer.')">
<button style="background:var(--mal);padding:7px 14px;font-size:13px">Eliminar paciente</button></form>{% endif %}
</div>
{% if p.alergias %}<div class=msg style="background:#fee2e2;border-left-color:var(--mal)"><b>⚠ Alergias:</b> {{p.alergias}}</div>{% endif %}
{% if proxima_cita %}<div class=msg style="background:#dbeafe;border-left-color:#2563eb"><b>📅 Próxima cita:</b> {{proxima_cita.fecha}} — {{proxima_cita.motivo or 'Sin motivo especificado'}}</div>
{% else %}<div class=msg style="background:#f1f5f9;border-left-color:#94a3b8"><b>📅 Próxima cita:</b> No tiene citas agendadas.</div>{% endif %}
<div class=card>
<h2 style="margin-top:0">Antecedentes médicos</h2>
<p><b>Alergias:</b> {{p.alergias or 'Sin registrar'}}<br><b>Antecedentes / enfermedades de base:</b> {{p.antecedentes or 'Sin registrar'}}</p>
{% if session.rol in ['odontologo','admin','recepcion'] %}<form method=post action="/pacientes/{{p.id}}/antecedentes" class=row>
<input name=alergias placeholder="Alergias (ej: penicilina)" value="{{p.alergias or ''}}" style="flex:1">
<input name=antecedentes placeholder="Antecedentes / enfermedades de base" value="{{p.antecedentes or ''}}" style="flex:1">
<button>Guardar</button></form>{% endif %}
</div>
<h2>Odontograma / Plan de tratamiento</h2><div class=bar><i style="width:{{(100*hecho/tot) if tot else 0}}%"></i></div>
<p>Hecho ${{'{:,.0f}'.format(hecho)}} de ${{'{:,.0f}'.format(tot)}} ({{plan|selectattr('estado','equalto','hecho')|list|length}} de {{plan|length}} procedimientos)</p>
{% for etapa, items in plan_g %}<h3 style="margin:14px 0 4px;font-size:15px;color:var(--azul-osc)">{{etapa}}</h3>
<table><tr><th>Diente<th>Tratamiento<th>Costo<th>Programado<th>Estado<th>Notas</tr>{% for x in items %}<tr><td>{{x.diente}}<td>{{x.tratamiento}}<td>${{'{:,.0f}'.format(x.costo or 0)}}
<td>{{x.fecha_prog or '-'}}<td><form method=post action=/plan/{{x.id}}><select name=estado onchange=this.form.submit()>{% for e in ['pendiente','programado','en curso','hecho'] %}<option {{'selected' if e==x.estado}}>{{e}}</option>{% endfor %}</select></form><td>{{x.notas or ''}}</tr>{% endfor %}</table>{% endfor %}
<form method=post action=/pacientes/{{p.id}}/plan class=row><input name=diente placeholder=Diente size=6><input name=tratamiento placeholder=Tratamiento required>
<input name=etapa placeholder="Etapa (ej: Fase 1 - Urgencias)" size=22><input name=costo type=number step=1000 placeholder=Costo>
<input name=fecha_prog type=date title="Fecha programada"><button>Agregar al plan</button></form>
<h2>Laboratorio</h2><table><tr><th>Laboratorio<th>Trabajo<th>Enviado<th>Entrega<th>Estado</tr>{% for l in labs %}<tr><td>{{l.laboratorio}}<td>{{l.trabajo}}<td>{{l.enviado}}<td>{{l.entrega}}
<td><form method=post action=/labs/{{l.id}}><select name=estado onchange=this.form.submit()>{% for e in ['enviado','en proceso','recibido','instalado'] %}<option {{'selected' if e==l.estado}}>{{e}}</option>{% endfor %}</select></form></tr>{% endfor %}</table>
<form method=post action=/pacientes/{{p.id}}/lab class=row><input name=laboratorio placeholder=Laboratorio required><input name=trabajo placeholder=Trabajo required>
<input name=entrega type=date title="Fecha de entrega"><button>Registrar envío</button></form>
<h2>Recetas</h2><table><tr><th>Folio<th>Fecha<th>Diagnóstico<th>Medicamentos<th></tr>{% for r in recetas %}<tr><td>{{r.folio}}<td>{{r.fecha}}<td>{{r.diagnostico}}<td>{{r.resumen}}<td><a href=/recetas/{{r.id}}>Ver / imprimir</a></tr>{% endfor %}</table>
{% if session.rol in ['odontologo','admin'] %}<p><a class=btn href=/pacientes/{{p.id}}/receta-nueva>Crear receta</a></p>{% endif %}
<h2>Informes médicos</h2><table><tr><th>Folio<th>Fecha<th>Título<th></tr>{% for inf in informes %}<tr><td>{{inf.folio}}<td>{{inf.fecha}}<td>{{inf.titulo}}<td><a href=/informes/{{inf.id}}>Ver / imprimir</a></tr>{% else %}<tr><td colspan=4>Sin informes registrados.</tr>{% endfor %}</table>
{% if session.rol in ['odontologo','admin'] %}<p><a class=btn href=/pacientes/{{p.id}}/informe-nuevo>Crear informe</a></p>{% endif %}
<h2>Radiografías</h2><table><tr><th>Fecha<th>Descripción<th>Subida por<th></tr>{% for x in rx %}<tr><td>{{x.fecha}}<td>{{x.descripcion}}<td>{{x.subido_por}}<td><a href=/rx/{{x.archivo}}>Descargar</a></tr>{% endfor %}</table>
<form method=post action=/pacientes/{{p.id}}/rx enctype=multipart/form-data class=row><input type=file name=archivo required><input name=descripcion placeholder=Descripción><button>Subir radiografía</button></form>
<h2>Documentos</h2><table><tr><th>Fecha<th>Archivo<th>Descripción<th>Subido por<th></tr>{% for d in documentos %}<tr><td>{{d.fecha}}<td>{{d.nombre_original}}<td>{{d.descripcion or '-'}}<td>{{d.subido_por}}
<td><a href=/documentos/{{d.archivo}}>Descargar</a> <form method=post action="/documentos/{{d.id}}/eliminar" onsubmit="return confirm('¿Eliminar este documento?')" style="display:inline;margin:0"><button style="background:var(--mal);padding:3px 8px;font-size:12px">Eliminar</button></form></tr>
{% else %}<tr><td colspan=5>Sin documentos adjuntos.</tr>{% endfor %}</table>
<form method=post action=/pacientes/{{p.id}}/documento enctype=multipart/form-data class=row><input type=file name=archivo required><input name=descripcion placeholder="Descripción (ej: consentimiento firmado, orden médica)" style="flex:1"><button>Subir documento</button></form>
<div class=card style="text-align:center;background:linear-gradient(135deg,#2563eb,#1e3a8a)">
<a href="/pacientes/{{p.id}}/odontograma" style="color:#fff;text-decoration:none;font-size:18px;font-weight:700">🦷 Ver odontograma gráfico</a>
</div>
<h2>Notas clínicas</h2><table><tr><th>Fecha<th>Usuario<th>Nota</tr>{% for n in notas %}<tr><td>{{n.fecha}}<td>{{n.usuario}}<td>{{n.texto}}</tr>{% else %}<tr><td colspan=3>Sin notas.</tr>{% endfor %}</table>
{% if session.rol in ['odontologo','admin'] %}<form method=post action="/pacientes/{{p.id}}/notas" class=row><input name=texto placeholder="Escribir nota clínica" style="flex:1" required><button>Agregar nota</button></form>{% endif %}
<h2>Cotizaciones</h2><table><tr><th>Fecha<th>Detalle<th>Total</tr>{% for c in cotizaciones %}<tr><td>{{c.fecha}}<td>{{c.items}}<td>${{'{:,.0f}'.format(c.total or 0)}}</tr>{% else %}<tr><td colspan=3>Sin cotizaciones.</tr>{% endfor %}</table>
{% if session.rol in ['odontologo','admin','recepcion'] %}<form method=post action="/pacientes/{{p.id}}/cotizacion" class=row><input name=items placeholder="Detalle del tratamiento cotizado" style="flex:1" required><input name=total type=number step=1000 placeholder=Total required><button>Guardar cotización</button></form>{% endif %}
<h2>Cuentas por cobrar</h2><table><tr><th>Fecha<th>Descripción<th>Total<th>Abonado<th>Saldo<th>Vence<th>Estado<th>Acción</tr>
{% for cu in cuentas %}<tr{% if cu.estado=='abierta' and cu.fecha_vence and cu.fecha_vence < hoy_iso %} style="background:#fee2e2"{% endif %}>
<td>{{cu.fecha}}<td>{{cu.descripcion}}<td>${{'{:,.0f}'.format(cu.total or 0)}}<td>${{'{:,.0f}'.format(cu.abonado or 0)}}<td>${{'{:,.0f}'.format((cu.total or 0)-(cu.abonado or 0))}}
<td>{{cu.fecha_vence or '-'}}{% if cu.estado=='abierta' and cu.fecha_vence and cu.fecha_vence < hoy_iso %} <b style="color:var(--mal)">¡Vencida!</b>{% endif %}
<td>{{cu.estado}}<td>{% if cu.estado=='pagada' and session.rol in ['admin','recepcion'] %}<form method=post action="/cuentas/{{cu.id}}/facturar" style="margin:0"><button style="padding:4px 8px;font-size:12px">Facturar</button></form>{% endif %}</tr>
{% else %}<tr><td colspan=8>Sin cuentas por cobrar.</tr>{% endfor %}</table>
<p>Saldo total pendiente: ${{'{:,.0f}'.format(saldo_total)}}</p>

<h2>Abonos y cartera</h2><table><tr><th>Fecha<th>Concepto<th>Monto</tr>{% for a in abonos %}<tr><td>{{a.fecha}}<td>{{a.concepto}}<td>${{'{:,.0f}'.format(a.monto or 0)}}</tr>{% else %}<tr><td colspan=3>Sin abonos registrados.</tr>{% endfor %}</table>
<p>Total abonado: ${{'{:,.0f}'.format(total_abonos)}}</p>
{% if session.rol in ['odontologo','admin','recepcion'] %}<form method=post action="/pacientes/{{p.id}}/abono" class=row><input name=concepto placeholder=Concepto required><input name=monto type=number step=1000 placeholder=Monto required>
<select name=cuenta_id><option value="">Sin asociar a cuenta</option>{% for cu in cuentas %}{% if cu.estado=='abierta' %}<option value="{{cu.id}}">{{cu.descripcion}} (saldo ${{'{:,.0f}'.format((cu.total or 0)-(cu.abonado or 0))}})</option>{% endif %}{% endfor %}</select>
<button>Registrar abono</button></form>{% endif %}
<h2>Consentimientos informados</h2><table><tr><th>Fecha<th>Procedimiento<th>Firmado por<th>Registrado por</tr>
{% for c in consentimientos %}<tr><td>{{c.fecha}}<td>{{c.procedimiento}}<td>{{c.firmado_por}}<td>{{c.usuario}}</tr>{% else %}<tr><td colspan=4>Sin consentimientos registrados.</tr>{% endfor %}</table>
{% if session.rol in ['odontologo','admin'] %}<form method=post action="/pacientes/{{p.id}}/consentimiento" class=row style="flex-direction:column;align-items:stretch">
<input name=procedimiento placeholder="Procedimiento a realizar (ej: Extracción, Endodoncia, Implante)" required>
<textarea name=texto rows=3 placeholder="Texto del consentimiento: riesgos, alternativas y que el paciente entendió y acepta el procedimiento"></textarea>
<input name=firmado_por placeholder="Nombre de quien firma (paciente o acudiente)" required>
<button style="align-self:flex-start">Guardar consentimiento</button></form>
<p style="font-size:12.5px;color:var(--mute)">Esto deja constancia escrita; para firma manuscrita real, imprime este registro y que el paciente firme sobre el papel.</p>{% endif %}
<h2>Atenciones (reporte RIPS)</h2><table><tr><th>Fecha<th>CUPS<th>CIE-10<th>Valor<th>Usuario</tr>{% for a in atenciones %}<tr><td>{{a.fecha}}<td>{{a.cod_cups}} {{a.desc_cups}}<td>{{a.cod_cie10}} {{a.desc_cie10}}<td>${{'{:,.0f}'.format(a.valor or 0)}}<td>{{a.usuario}}</tr>{% endfor %}</table>
{% if session.rol in ['odontologo','admin'] %}<form method=post action=/pacientes/{{p.id}}/atencion class=row>
<input name=cod_cups placeholder="Código CUPS" required size=8><input name=desc_cups placeholder="Procedimiento" required size=22>
<input name=cod_cie10 placeholder="Código CIE-10" required size=8><input name=desc_cie10 placeholder="Diagnóstico" required size=22>
<select name=tipo_diagnostico><option value=1>Impresión diagnóstica</option><option value=2>Confirmado nuevo</option><option value=3>Confirmado repetido</option></select>
<input name=valor type=number step=1000 placeholder=Valor required><input name=num_factura placeholder="N° factura" size=10><button>Registrar atención</button></form>{% endif %}""",
        p=p, plan=plan, plan_g=plan_g, tot=tot, hecho=hecho, proxima_cita=proxima_cita,
        labs=q("SELECT * FROM labs WHERE patient_id=? ORDER BY id DESC", (pid,)),
        recetas=q("""SELECT r.*, (SELECT GROUP_CONCAT(medicamento,', ') FROM receta_items WHERE receta_id=r.id) resumen
                      FROM recetas r WHERE patient_id=? ORDER BY id DESC""", (pid,)),
        rx=q("SELECT * FROM rx WHERE patient_id=? ORDER BY id DESC", (pid,)),
        atenciones=q("SELECT * FROM atenciones WHERE patient_id=? ORDER BY id DESC", (pid,)),
        notas=q("SELECT * FROM notes WHERE patient_id=? ORDER BY id DESC", (pid,)),
        cotizaciones=q("SELECT * FROM quotes WHERE patient_id=? ORDER BY id DESC", (pid,)),
        abonos=q("SELECT * FROM abonos WHERE patient_id=? ORDER BY id DESC", (pid,)),
        total_abonos=sum(a["monto"] or 0 for a in q("SELECT * FROM abonos WHERE patient_id=?", (pid,))),
        consentimientos=q("SELECT * FROM consentimientos WHERE patient_id=? ORDER BY id DESC", (pid,)),
        cuentas=q("SELECT * FROM cuentas WHERE patient_id=? ORDER BY id DESC", (pid,)),
        saldo_total=sum((x["total"] or 0) - (x["abonado"] or 0) for x in q("SELECT * FROM cuentas WHERE patient_id=? AND estado='abierta'", (pid,))),
        hoy_iso=dt.date.today().isoformat(),
        documentos=q("SELECT * FROM documentos WHERE patient_id=? ORDER BY id DESC", (pid,)),
        informes=q("SELECT * FROM informes WHERE patient_id=? ORDER BY id DESC", (pid,)))

@app.post("/pacientes/<int:pid>/plan")
@need("odontologo")
def plan_add(pid):
    f = request.form
    costo = float(f["costo"] or 0)
    plan_id = ex("INSERT INTO plan(patient_id,diente,tratamiento,etapa,costo,fecha_prog) VALUES(?,?,?,?,?,?)",
       (pid, f["diente"], f["tratamiento"], f.get("etapa") or "Sin etapa", costo, f.get("fecha_prog") or None))
    if costo > 0:
        vence = (dt.date.today() + dt.timedelta(days=30)).isoformat()
        ex("INSERT INTO cuentas(patient_id,plan_id,fecha,descripcion,total,abonado,fecha_vence,estado) VALUES(?,?,?,?,?,0,?,'abierta')",
           (pid, plan_id, dt.date.today().isoformat(), f["tratamiento"], costo, vence))
    return redirect(f"/pacientes/{pid}")

@app.post("/plan/<int:i>")
@need("odontologo")
def plan_estado(i):
    hecho = dt.date.today().isoformat() if request.form["estado"] == "hecho" else None
    ex("UPDATE plan SET estado=?,fecha_hecho=? WHERE id=?", (request.form["estado"], hecho, i))
    return redirect(f"/pacientes/{q('SELECT patient_id p FROM plan WHERE id=?', (i,), one=True)['p']}")

@app.post("/pacientes/<int:pid>/lab")
@need("odontologo", "recepcion")
def lab_add(pid):
    f = request.form
    ex("INSERT INTO labs(patient_id,laboratorio,trabajo,enviado,entrega) VALUES(?,?,?,?,?)",
       (pid, f["laboratorio"], f["trabajo"], dt.date.today().isoformat(), f["entrega"]))
    return redirect(f"/pacientes/{pid}")

@app.post("/labs/<int:i>")
@need("odontologo", "recepcion")
def lab_estado(i):
    ex("UPDATE labs SET estado=? WHERE id=?", (request.form["estado"], i))
    return redirect(f"/pacientes/{q('SELECT patient_id p FROM labs WHERE id=?', (i,), one=True)['p']}")

@app.route("/pacientes/<int:pid>/receta-nueva", methods=["GET", "POST"])
@need("odontologo")
def receta_nueva(pid):
    p = q("SELECT * FROM patients WHERE id=?", (pid,), one=True) or abort(404)
    if request.method == "POST":
        f = request.form
        folio = f"RX-{dt.datetime.now():%Y%m%d%H%M%S}"
        rid = ex("INSERT INTO recetas(patient_id,fecha,autor,diagnostico,folio) VALUES(?,?,?,?,?)",
                 (pid, dt.date.today().isoformat(), session["u"], f.get("diagnostico", ""), folio))
        meds = f.getlist("medicamento"); dosis = f.getlist("dosis"); via = f.getlist("via")
        frec = f.getlist("frecuencia"); dur = f.getlist("duracion"); ind = f.getlist("indicaciones")
        for i, m in enumerate(meds):
            if m.strip():
                ex("INSERT INTO receta_items(receta_id,medicamento,dosis,via,frecuencia,duracion,indicaciones) VALUES(?,?,?,?,?,?,?)",
                   (rid, m, dosis[i], via[i], frec[i], dur[i], ind[i]))
        audit("crear_receta", f"paciente {pid} folio {folio}")
        return redirect(f"/recetas/{rid}")
    return page("Nueva receta", """<h1>Nueva receta - {{p.nombre}}</h1>
<form method=post><p><input name=diagnostico placeholder="Diagnóstico" size=50></p>
<div id=items><div class=row><input name=medicamento placeholder="Medicamento" required><input name=dosis placeholder=Dosis size=8>
<input name=via placeholder=Vía size=10><input name=frecuencia placeholder=Frecuencia size=10><input name=duracion placeholder="Duración" size=10>
<input name=indicaciones placeholder=Indicaciones size=22></div></div>
<p><button type=button onclick="document.getElementById('items').insertAdjacentHTML('beforeend',document.getElementById('items').firstElementChild.outerHTML)">+ Agregar medicamento</button></p>
<button>Guardar receta</button></form>""", p=p)

@app.route("/recetas/<int:i>")
@need("odontologo", "recepcion")
def receta(i):
    r = q("SELECT r.*,p.nombre,p.documento FROM recetas r JOIN patients p ON p.id=r.patient_id WHERE r.id=?", (i,), one=True) or abort(404)
    items = q("SELECT * FROM receta_items WHERE receta_id=?", (i,))
    return page("Receta", """<h1>Fórmula médica odontológica</h1><p>Dra. Natalia Morera Odontología Especializada<br>Folio: {{r.folio}} · Fecha: {{r.fecha}}</p>
<p><b>Paciente:</b> {{r.nombre}} ({{r.documento}})</p>{% if r.diagnostico %}<p><b>Diagnóstico:</b> {{r.diagnostico}}</p>{% endif %}
<table><tr><th>Medicamento<th>Dosis<th>Vía<th>Frecuencia<th>Duración<th>Indicaciones</tr>
{% for x in items %}<tr><td>{{x.medicamento}}<td>{{x.dosis}}<td>{{x.via}}<td>{{x.frecuencia}}<td>{{x.duracion}}<td>{{x.indicaciones}}</tr>{% endfor %}</table>
<p>Prescrito por: {{r.autor}}<br>Firma y registro profesional: ____________________</p><button class=noprint onclick=print()>Imprimir</button>""", r=r, items=items)

@app.route("/pacientes/<int:pid>/informe-nuevo", methods=["GET", "POST"])
@need("odontologo")
def informe_nuevo(pid):
    p = q("SELECT * FROM patients WHERE id=?", (pid,), one=True) or abort(404)
    if request.method == "POST":
        f = request.form
        folio = f"INF-{dt.datetime.now():%Y%m%d%H%M%S}"
        iid = ex("INSERT INTO informes(patient_id,fecha,titulo,contenido,autor,folio) VALUES(?,?,?,?,?,?)",
                 (pid, dt.date.today().isoformat(), f.get("titulo", "").strip() or "Informe médico", f.get("contenido", "").strip(), session["u"], folio))
        audit("crear_informe", f"paciente {pid} folio {folio}")
        return redirect(f"/informes/{iid}")
    return page("Nuevo informe", """<h1>Nuevo informe médico - {{p.nombre}}</h1>
<form method=post><p><input name=titulo placeholder="Título (ej: Informe de evolución, Remisión, Concepto odontológico)" size=50 required></p>
<p><textarea name=contenido rows=10 style="width:100%" placeholder="Contenido del informe" required></textarea></p>
<button>Guardar informe</button></form>""", p=p)

@app.route("/informes/<int:i>")
@need("odontologo", "recepcion")
def informe(i):
    r = q("SELECT inf.*,p.nombre,p.documento FROM informes inf JOIN patients p ON p.id=inf.patient_id WHERE inf.id=?", (i,), one=True) or abort(404)
    return page("Informe", """<h1>{{r.titulo}}</h1><p>Dra. Natalia Morera Odontología Especializada<br>Folio: {{r.folio}} · Fecha: {{r.fecha}}</p>
<p><b>Paciente:</b> {{r.nombre}} ({{r.documento}})</p>
<p style="white-space:pre-wrap">{{r.contenido}}</p>
<p>Elaborado por: {{r.autor}}<br>Firma y registro profesional: ____________________</p><button class=noprint onclick=print()>Imprimir</button>""", r=r)

@app.post("/pacientes/<int:pid>/rx")
@need("odontologo", "radiologo")
def rx_add(pid):
    f = request.files["archivo"]
    name = f"{pid}_{dt.datetime.now():%Y%m%d%H%M%S}_{secure_filename(f.filename)}"
    ok_guardado = storage_guardar(name, f.read(), f.mimetype or "application/octet-stream")
    if not ok_guardado:
        flash("No se pudo guardar el archivo. Intenta de nuevo.")
        return redirect(f"/pacientes/{pid}")
    ex("INSERT INTO rx(patient_id,archivo,descripcion,subido_por,fecha) VALUES(?,?,?,?,?)",
       (pid, name, request.form["descripcion"], session["u"], dt.date.today().isoformat()))
    audit("subir_rx", f"paciente {pid}: {name}")
    return redirect(f"/pacientes/{pid}")

# Marca de agua JKT innova (imagen incrustada, no depende de archivos externos)
MARCA_B64 = "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"

@app.route("/marca")
def marca():
    import base64 as _b64
    r = app.response_class(_b64.b64decode(MARCA_B64), mimetype="image/jpeg")
    r.headers["Cache-Control"] = "public, max-age=86400"
    return r

@app.route("/logo")
def logo():
    row = q("SELECT value FROM settings WHERE key='logo_file'", one=True)
    if not row or not row["value"]: abort(404)
    datos, encontrado = storage_leer(row["value"])
    if not encontrado: abort(404)
    ext = os.path.splitext(row["value"])[1].lower()
    tipos = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml", ".webp": "image/webp"}
    return Response(datos, mimetype=tipos.get(ext, "application/octet-stream"))

@app.route("/foto/<int:pid>")
@need("odontologo", "admin", "recepcion", "radiologo")
def foto_paciente(pid):
    p = q("SELECT foto FROM patients WHERE id=?", (pid,), one=True)
    if not p or not p["foto"]: abort(404)
    datos, encontrado = storage_leer(p["foto"])
    if not encontrado: abort(404)
    ext = os.path.splitext(p["foto"])[1].lower()
    tipos = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
    return Response(datos, mimetype=tipos.get(ext, "application/octet-stream"))

@app.post("/pacientes/<int:pid>/foto")
@need("odontologo", "admin", "recepcion")
def foto_paciente_actualizar(pid):
    foto = request.files.get("foto")
    if foto and foto.filename:
        ext = os.path.splitext(foto.filename)[1].lower()
        tipos_foto = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
        if ext in tipos_foto:
            nombre_foto = f"foto_paciente_{pid}{ext}"
            if storage_guardar(nombre_foto, foto.read(), tipos_foto[ext]):
                ex("UPDATE patients SET foto=? WHERE id=?", (nombre_foto, pid))
                flash("Fotografía actualizada.")
            else:
                flash("No se pudo guardar la fotografía.")
        else:
            flash("Formato de imagen no válido. Usa PNG, JPG o WEBP.")
    return redirect(f"/pacientes/{pid}")

@app.route("/rx/<path:n>")
@need("odontologo", "radiologo", "recepcion")
def rx_get(n):
    datos, encontrado = storage_leer(n)
    if not encontrado: abort(404)
    return Response(datos, mimetype="application/octet-stream",
                     headers={"Content-Disposition": f'attachment; filename="{n}"'})

@app.post("/pacientes/<int:pid>/documento")
@need("odontologo", "admin", "recepcion")
def documento_add(pid):
    f = request.files.get("archivo")
    if not f or not f.filename:
        flash("Selecciona un archivo.")
        return redirect(f"/pacientes/{pid}")
    nombre = f"doc_{pid}_{dt.datetime.now():%Y%m%d%H%M%S}_{secure_filename(f.filename)}"
    if not storage_guardar(nombre, f.read(), f.mimetype or "application/octet-stream"):
        flash("No se pudo guardar el documento. Intenta de nuevo.")
        return redirect(f"/pacientes/{pid}")
    ex("INSERT INTO documentos(patient_id,archivo,nombre_original,descripcion,subido_por,fecha) VALUES(?,?,?,?,?,?)",
       (pid, nombre, f.filename, request.form.get("descripcion", "").strip(), session["u"], dt.date.today().isoformat()))
    audit("subir_documento", f"paciente {pid}: {f.filename}")
    return redirect(f"/pacientes/{pid}")

@app.route("/documentos/<path:n>")
@need("odontologo", "admin", "recepcion", "radiologo")
def documento_get(n):
    fila = q("SELECT nombre_original FROM documentos WHERE archivo=?", (n,), one=True)
    nombre_descarga = fila["nombre_original"] if fila else n
    datos, encontrado = storage_leer(n)
    if not encontrado: abort(404)
    return Response(datos, mimetype="application/octet-stream",
                     headers={"Content-Disposition": f'attachment; filename="{nombre_descarga}"'})

@app.post("/documentos/<int:did>/eliminar")
@need("odontologo", "admin", "recepcion")
def documento_eliminar(did):
    doc = q("SELECT * FROM documentos WHERE id=?", (did,), one=True) or abort(404)
    storage_borrar(doc["archivo"])
    ex("DELETE FROM documentos WHERE id=?", (did,))
    audit("eliminar_documento", f"paciente {doc['patient_id']}: {doc['nombre_original']}")
    return redirect(f"/pacientes/{doc['patient_id']}")

@app.post("/pacientes/<int:pid>/eliminar")
@need("admin", "recepcion")
def paciente_eliminar(pid):
    p = q("SELECT * FROM patients WHERE id=?", (pid,), one=True) or abort(404)
    for r in q("SELECT archivo FROM rx WHERE patient_id=?", (pid,)):
        storage_borrar(r["archivo"])
    for d in q("SELECT archivo FROM documentos WHERE patient_id=?", (pid,)):
        storage_borrar(d["archivo"])
    if p["foto"]:
        storage_borrar(p["foto"])
    ex("DELETE FROM receta_items WHERE receta_id IN (SELECT id FROM recetas WHERE patient_id=?)", (pid,))
    for t in ("citas", "plan", "labs", "recetas", "rx", "atenciones", "notes", "quotes", "abonos", "consentimientos", "cuentas", "documentos", "informes"):
        ex(f"DELETE FROM {t} WHERE patient_id=?", (pid,))
    # Ventas y movimientos de caja se conservan (contabilidad), solo se desvinculan del paciente
    ex("UPDATE sales SET patient_id=NULL WHERE patient_id=?", (pid,))
    ex("UPDATE caja_mov SET patient_id=NULL WHERE patient_id=?", (pid,))
    ex("DELETE FROM patients WHERE id=?", (pid,))
    audit("eliminar_paciente", f"{p['nombre']} (id {pid})")
    flash(f"Paciente {p['nombre']} eliminado.")
    return redirect("/pacientes")

@app.post("/pacientes/<int:pid>/antecedentes")
@need("odontologo", "admin", "recepcion")
def antecedentes_add(pid):
    f = request.form
    ex("UPDATE patients SET alergias=?, antecedentes=? WHERE id=?",
       (f.get("alergias", "").strip(), f.get("antecedentes", "").strip(), pid))
    audit("antecedentes", f"paciente {pid}")
    return redirect(f"/pacientes/{pid}")

@app.post("/pacientes/<int:pid>/notas")
@need("odontologo", "admin")
def nota_add(pid):
    texto = request.form["texto"].strip()
    ex("INSERT INTO notes(patient_id,usuario,fecha,texto) VALUES(?,?,?,?)",
       (pid, session["u"], dt.datetime.now().isoformat(timespec="seconds"), texto))
    audit("nota_clinica", f"paciente {pid}")
    return redirect(f"/pacientes/{pid}")

@app.post("/pacientes/<int:pid>/cotizacion")
@need("odontologo", "admin", "recepcion")
def cotizacion_add(pid):
    f = request.form
    ex("INSERT INTO quotes(patient_id,fecha,items,total,usuario) VALUES(?,?,?,?,?)",
       (pid, dt.date.today().isoformat(), f["items"].strip(), float(f["total"] or 0), session["u"]))
    audit("cotizacion", f"paciente {pid}")
    return redirect(f"/pacientes/{pid}")

@app.post("/pacientes/<int:pid>/abono")
@need("odontologo", "admin", "recepcion")
def abono_add(pid):
    f = request.form
    monto = float(f["monto"] or 0)
    ex("INSERT INTO abonos(patient_id,fecha,monto,concepto,usuario) VALUES(?,?,?,?,?)",
       (pid, dt.date.today().isoformat(), monto, f["concepto"].strip(), session["u"]))
    cuenta_id = f.get("cuenta_id")
    if cuenta_id:
        cu = q("SELECT * FROM cuentas WHERE id=?", (cuenta_id,), one=True)
        if cu:
            nuevo_abonado = (cu["abonado"] or 0) + monto
            nuevo_estado = "pagada" if nuevo_abonado >= (cu["total"] or 0) else "abierta"
            ex("UPDATE cuentas SET abonado=?, estado=? WHERE id=?", (nuevo_abonado, nuevo_estado, cuenta_id))
    audit("abono", f"paciente {pid}: {f['concepto']}")
    return redirect(f"/pacientes/{pid}")

@app.post("/cuentas/<int:cid>/facturar")
@need("admin", "recepcion")
def cuenta_facturar(cid):
    cu = q("SELECT * FROM cuentas WHERE id=?", (cid,), one=True) or abort(404)
    ex("UPDATE cuentas SET estado='facturada' WHERE id=?", (cid,))
    audit("cuenta_facturada", f"cuenta {cid}, paciente {cu['patient_id']}")
    return redirect(f"/pacientes/{cu['patient_id']}")

@app.post("/pacientes/<int:pid>/consentimiento")
@need("odontologo", "admin")
def consentimiento_add(pid):
    f = request.form
    ex("INSERT INTO consentimientos(patient_id,fecha,procedimiento,texto,firmado_por,usuario) VALUES(?,?,?,?,?,?)",
       (pid, dt.date.today().isoformat(), f["procedimiento"].strip(), f.get("texto", "").strip(), f["firmado_por"].strip(), session["u"]))
    audit("consentimiento", f"paciente {pid}: {f['procedimiento']}")
    flash("Consentimiento registrado.")
    return redirect(f"/pacientes/{pid}")

@app.post("/pacientes/<int:pid>/atencion")
@need("odontologo")
def atencion_add(pid):
    f = request.form
    now = dt.datetime.now()
    ex("""INSERT INTO atenciones(patient_id,fecha,hora,usuario,cod_cups,desc_cups,cod_cie10,desc_cie10,tipo_diagnostico,valor,num_factura,created)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
       (pid, now.date().isoformat(), now.strftime("%H:%M:%S"), session["u"], f["cod_cups"].strip(), f["desc_cups"].strip(),
        f["cod_cie10"].strip().upper(), f["desc_cie10"].strip(), f.get("tipo_diagnostico", "2"), float(f["valor"] or 0),
        f.get("num_factura", ""), now.isoformat(timespec="seconds")))
    audit("registrar_atencion", f"paciente {pid} CUPS {f['cod_cups']} CIE10 {f['cod_cie10']}")
    return redirect(f"/pacientes/{pid}")

@app.route("/citas", methods=["GET", "POST"])
@need("odontologo", "recepcion")
def citas():
    if request.method == "POST":
        f = request.form
        ex("INSERT INTO citas(patient_id,fecha,motivo,estado,dentist_id) VALUES(?,?,?,'confirmada',?)",
           (f["patient_id"], f["fecha"].replace("T", " "), f["motivo"], f.get("dentist_id") or None))
    cs = q("""SELECT c.*,p.nombre,d.name dentist_name FROM citas c LEFT JOIN patients p ON p.id=c.patient_id
              LEFT JOIN dentists d ON d.id=c.dentist_id ORDER BY fecha DESC LIMIT 100""")

    hoy = dt.date.today()
    try:
        anio = int(request.args.get("anio", hoy.year))
        mes = int(request.args.get("mes", hoy.month))
    except ValueError:
        anio, mes = hoy.year, hoy.month
    mes_ant = (anio, mes - 1) if mes > 1 else (anio - 1, 12)
    mes_sig = (anio, mes + 1) if mes < 12 else (anio + 1, 1)
    nombres_mes = ["", "Enero","Febrero","Marzo","Abril","Mayo","Junio","Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"]

    ym = f"{anio:04d}-{mes:02d}"
    citas_mes = q("""SELECT c.*,COALESCE(p.nombre,c.nombre_libre) nombre FROM citas c LEFT JOIN patients p ON p.id=c.patient_id
                     WHERE c.fecha LIKE ? ORDER BY c.fecha""", (ym + "%",))
    por_dia = {}
    for cc in citas_mes:
        dia = int(cc["fecha"][8:10])
        por_dia.setdefault(dia, []).append(cc)

    primer_dia, num_dias = calendar.monthrange(anio, mes)
    semanas = []
    semana = [None] * primer_dia
    for d in range(1, num_dias + 1):
        semana.append(d)
        if len(semana) == 7:
            semanas.append(semana); semana = []
    if semana:
        semana += [None] * (7 - len(semana))
        semanas.append(semana)

    celdas_html = ""
    colores_estado = {
        "cancelada":  {"bg": "linear-gradient(135deg,#fecaca,#fca5a5)", "fg": "#7f1d1d", "dot": "#ef4444"},
        "atendida":   {"bg": "linear-gradient(135deg,#bbf7d0,#86efac)", "fg": "#14532d", "dot": "#22c55e"},
        "confirmada": {"bg": "linear-gradient(135deg,#bfdbfe,#93c5fd)", "fg": "#1e3a8a", "dot": "#3b82f6"},
        "pendiente":  {"bg": "linear-gradient(135deg,#fde68a,#fcd34d)", "fg": "#78350f", "dot": "#f59e0b"},
    }
    for semana in semanas:
        celdas_html += "<tr>"
        for d in semana:
            if d is None:
                celdas_html += "<td style='background:#f8fafc;border:none'></td>"
                continue
            es_hoy = (d == hoy.day and mes == hoy.month and anio == hoy.year)
            citas_dia = por_dia.get(d, [])
            lista = "".join(
                f'<div style="font-size:11px;padding:3px 6px;margin:2px 0;border-radius:6px;'
                f'background:{colores_estado.get(x["estado"], colores_estado["pendiente"])["bg"]};'
                f'color:{colores_estado.get(x["estado"], colores_estado["pendiente"])["fg"]};'
                f'font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;'
                f'box-shadow:0 1px 2px rgba(0,0,0,.08)" title="{x["fecha"][11:16]} - {x["nombre"] or ""} ({x["estado"]})">'
                f'&#9679; {x["fecha"][11:16]} {x["nombre"] or ""}</div>'
                for x in citas_dia
            )
            estilo_celda = ("background:linear-gradient(160deg,#ecfeff,#cffafe);"
                             "box-shadow:inset 0 0 0 2px #06b6d4;") if es_hoy else "background:#ffffff;"
            numero_dia = (f'<span style="display:inline-flex;align-items:center;justify-content:center;'
                          f'width:24px;height:24px;border-radius:50%;background:#06b6d4;color:#fff;'
                          f'font-weight:700;font-size:12px">{d}</span>') if es_hoy else f'<b style="font-size:12px;color:#334155">{d}</b>'
            celdas_html += f'<td style="vertical-align:top;height:86px;border-radius:10px;padding:4px;{estilo_celda}">{numero_dia}{lista}</td>'
        celdas_html += "</tr>"

    calendario_html = (
        f'<div class=card style="background:linear-gradient(160deg,#f0f9ff,#ffffff);border-radius:16px;padding:18px;box-shadow:0 4px 18px rgba(2,132,199,.08)">'
        f'<div class=row style="justify-content:space-between;align-items:center">'
        f'<a class=btn style="background:#0ea5e9;color:#fff;border-radius:8px" href="/citas?anio={mes_ant[0]}&mes={mes_ant[1]}">&larr; Anterior</a>'
        f'<h3 style="margin:0;background:linear-gradient(90deg,#0ea5e9,#6366f1);-webkit-background-clip:text;background-clip:text;color:transparent;font-size:22px">{nombres_mes[mes]} {anio}</h3>'
        f'<a class=btn style="background:#0ea5e9;color:#fff;border-radius:8px" href="/citas?anio={mes_sig[0]}&mes={mes_sig[1]}">Siguiente &rarr;</a></div>'
        f'<table style="margin-top:12px;border-collapse:separate;border-spacing:4px">'
        f'<tr>{"".join(f"<th style=\'background:#0ea5e9;color:#fff;border-radius:6px;padding:4px\'>{dnom}</th>" for dnom in ["Lun","Mar","Mié","Jue","Vie","Sáb","Dom"])}</tr>'
        f'{celdas_html}</table>'
        f'<div class=row style="margin-top:10px;gap:14px;font-size:12px;color:#475569">'
        + "".join(f'<span><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{v["dot"]};margin-right:4px"></span>{k.capitalize()}</span>' for k, v in colores_estado.items())
        + '</div></div>'
    )

    return page("Citas", """<h1>Citas</h1>""" + calendario_html + """
<h2>Listado</h2><table><tr><th>Fecha<th>Paciente<th>Odontólogo<th>Motivo<th>Estado<th></tr>{% for c in cs %}<tr><td>{{c.fecha}}<td>{{c.nombre or c.nombre_libre}} {{c.telefono_libre or ''}}
<td>{{c.dentist_name or '—'}}<td>{{c.motivo}}<td>{{c.estado}} {{'(portal)' if c.origen=='portal'}}<td><form method=post action=/citas/{{c.id}} class=row style=margin:0>
<select name=estado>{% for e in ['pendiente','confirmada','atendida','cancelada'] %}<option {{'selected' if e==c.estado}}>{{e}}</option>{% endfor %}</select><button>Cambiar</button></form><form method=post action=/citas/{{c.id}}/eliminar style=margin:0 onsubmit="return confirm('¿Eliminar esta cita? Esta acción no se puede deshacer.')"><button style="background:#dc2626">Eliminar</button></form></tr>{% endfor %}</table>
<h2>Nueva cita</h2><form method=post class=row><select name=patient_id required>{% for p in ps %}<option value={{p.id}}>{{p.nombre}}</option>{% endfor %}</select>
<select name=dentist_id><option value="">Odontólogo (opcional)</option>{% for d in ds %}<option value={{d.id}}>{{d.name}}</option>{% endfor %}</select>
<input type=datetime-local name=fecha required><input name=motivo placeholder=Motivo><button>Agendar cita</button></form>""",
        cs=cs, ps=q("SELECT id,nombre FROM patients ORDER BY nombre"), ds=q("SELECT id,name FROM dentists WHERE active=1 ORDER BY name"))

@app.post("/citas/<int:i>")
@need("odontologo", "recepcion")
def cita_estado(i):
    ex("UPDATE citas SET estado=? WHERE id=?", (request.form["estado"], i)); return redirect("/citas")

@app.post("/citas/<int:i>/eliminar")
@need("odontologo", "recepcion")
def cita_eliminar(i):
    ex("DELETE FROM citas WHERE id=?", (i,))
    audit("eliminar_cita", f"cita {i}")
    flash("Cita eliminada.")
    return redirect("/citas")

@app.route("/recordatorios")
@need("odontologo", "recepcion")
def recordatorios():
    a, b = dt.datetime.now(), dt.datetime.now() + dt.timedelta(days=2)
    cs = q("""SELECT c.*,COALESCE(p.nombre,c.nombre_libre) n,COALESCE(p.telefono,c.telefono_libre) t,p.email FROM citas c LEFT JOIN patients p ON p.id=c.patient_id
              WHERE c.estado IN('pendiente','confirmada') AND c.fecha BETWEEN ? AND ? ORDER BY c.fecha""", (a.strftime("%Y-%m-%d %H:%M"), b.strftime("%Y-%m-%d %H:%M")))
    nota_email = "" if email_listo() else ('<p style="font-size:13px;color:var(--mute)">El envío de correo automático no está activo todavía. '
                                          'Ve a <a href="/configuracion">Configuración</a> y completa los datos de correo. El botón de WhatsApp sigue funcionando normal.</p>')
    return page("Recordatorios", """<h1>Citas de las próximas 48 horas</h1>""" + nota_email + """
<table><tr><th>Fecha<th>Paciente<th>Enviar</tr>{% for c in cs %}<tr><td>{{c.fecha}}<td>{{c.n}}
<td>{% set m='Hola '+c.n+', le recordamos su cita el '+c.fecha+' en Dra. Natalia Morera Odontología. Responda este mensaje para confirmar.' %}
{% if wa(c.t,m) %}<a class=btn href="{{wa(c.t,m)}}" target=_blank>WhatsApp</a>{% endif %}
{% if c.email %}<form method=post action="/recordatorios/{{c.id}}/correo" style="display:inline;margin:0"><button style="padding:5px 10px;font-size:12.5px">Enviar correo</button></form>{% endif %}
</tr>{% else %}<tr><td colspan=3>No hay citas próximas.</tr>{% endfor %}</table>
<h2>Recordatorios masivos por rango de fechas</h2>
<form method=post action="/recordatorios/masivo" class=row>
<label>Desde <input type=date name=desde required></label>
<label>Hasta <input type=date name=hasta required></label>
<button>Enviar recordatorio por correo a todas las citas del rango</button></form>""", cs=cs)

@app.post("/recordatorios/<int:cid>/correo")
@need("odontologo", "recepcion")
def recordatorio_correo(cid):
    c = q("""SELECT c.*,COALESCE(p.nombre,c.nombre_libre) n,p.email FROM citas c LEFT JOIN patients p ON p.id=c.patient_id
             WHERE c.id=?""", (cid,), one=True) or abort(404)
    asunto = "Recordatorio de su cita - Dra. Natalia Morera Odontología Especializada"
    cuerpo = f"Hola {c['n']},\n\nLe recordamos su cita el {c['fecha']} en Dra. Natalia Morera Odontología Especializada.\n\nSi necesita reprogramar, por favor contáctenos.\n\nGracias."
    ok_envio, error = enviar_correo(c["email"], asunto, cuerpo)
    flash("Correo de recordatorio enviado." if ok_envio else f"No se pudo enviar el correo: {error}")
    return redirect("/recordatorios")

@app.post("/recordatorios/masivo")
@need("odontologo", "recepcion")
def recordatorios_masivo():
    desde, hasta = request.form["desde"], request.form["hasta"]
    cs = q("""SELECT c.*,COALESCE(p.nombre,c.nombre_libre) n,p.email FROM citas c LEFT JOIN patients p ON p.id=c.patient_id
              WHERE c.estado IN('pendiente','confirmada') AND c.fecha BETWEEN ? AND ? ORDER BY c.fecha""",
             (desde + " 00:00", hasta + " 23:59"))
    enviados, sin_correo, fallidos = 0, 0, 0
    for c in cs:
        if not c["email"]:
            sin_correo += 1
            continue
        asunto = "Recordatorio de su cita - Dra. Natalia Morera Odontología Especializada"
        cuerpo = f"Hola {c['n']},\n\nLe recordamos su cita el {c['fecha']} en Dra. Natalia Morera Odontología Especializada.\n\nSi necesita reprogramar, por favor contáctenos.\n\nGracias."
        ok_envio, _ = enviar_correo(c["email"], asunto, cuerpo)
        if ok_envio: enviados += 1
        else: fallidos += 1
    audit("recordatorios_masivo", f"{desde} a {hasta}: {enviados} enviados, {sin_correo} sin correo, {fallidos} fallidos")
    flash(f"Recordatorios enviados: {enviados}. Sin correo registrado: {sin_correo}. Fallidos: {fallidos}.")
    return redirect("/recordatorios")

@app.route("/reactivacion")
@need("odontologo", "recepcion")
def reactivacion():
    lim = (dt.date.today() - dt.timedelta(days=180)).isoformat()
    ps = q("""SELECT * FROM (SELECT p.*,(SELECT MAX(substr(fecha,1,10)) FROM citas c WHERE c.patient_id=p.id AND c.estado='atendida') ultima FROM patients p) t
              WHERE (ultima IS NULL AND creado<?) OR ultima<? ORDER BY ultima""", (lim, lim))
    return page("Reactivar", """<h1>Pacientes sin visita en 6 meses</h1><table><tr><th>Paciente<th>Última visita<th>Contactar</tr>{% for p in ps %}<tr><td>{{p.nombre}}<td>{{p.ultima or 'sin visitas'}}
<td>{% set m='Hola '+p.nombre+', hace tiempo no nos visita. ¿Le agendamos su control en Dra. Natalia Morera Odontología?' %}{% if wa(p.telefono,m) %}<a class=btn href="{{wa(p.telefono,m)}}" target=_blank>WhatsApp</a>{% endif %}</tr>
{% else %}<tr><td colspan=3>Todos los pacientes están al día.</tr>{% endfor %}</table>""", ps=ps)

@app.route("/reservar", methods=["GET", "POST"])
def reservar():
    if request.method == "POST":
        f = request.form
        ex("INSERT INTO citas(nombre_libre,telefono_libre,fecha,motivo,estado,origen) VALUES(?,?,?,?, 'pendiente','portal')",
           (f["nombre"], f["telefono"], f["fecha"].replace("T", " "), f["motivo"]))
        return page("Reserva", "<h1>Solicitud recibida</h1><p>Te confirmaremos la cita por WhatsApp o llamada.</p>")
    return page("Reservar", """<h1>Reserva tu cita</h1><form method=post class=row style=flex-direction:column;max-width:380px>
<input name=nombre placeholder="Tu nombre" required><input name=telefono placeholder=Celular required><input type=datetime-local name=fecha required>
<input name=motivo placeholder="Motivo de la consulta"><button>Solicitar cita</button></form>""")

@app.route("/caja", methods=["GET", "POST"])
@need("odontologo", "recepcion")
def caja():
    abierta = q("SELECT * FROM caja WHERE estado='abierta' ORDER BY id DESC LIMIT 1", one=True)
    if request.method == "POST" and request.form.get("accion") == "abrir":
        if abierta:
            flash("Ya hay una caja abierta.")
        else:
            ex("INSERT INTO caja(fecha_apertura,monto_apertura,usuario_abre) VALUES(?,?,?)",
               (dt.datetime.now().isoformat(timespec="seconds"), float(request.form["monto"] or 0), session["u"]))
            audit("abrir_caja", request.form["monto"])
        return redirect("/caja")
    if request.method == "POST" and request.form.get("accion") == "mov":
        if not abierta:
            flash("Primero abre la caja.")
        else:
            f = request.form
            monto = float(f["monto"] or 0)
            ex("INSERT INTO caja_mov(caja_id,fecha,tipo,concepto,monto,usuario) VALUES(?,?,?,?,?,?)",
               (abierta["id"], dt.datetime.now().isoformat(timespec="seconds"), f["tipo"], f["concepto"], monto, session["u"]))
        return redirect("/caja")
    movs = q("SELECT * FROM caja_mov WHERE caja_id=? ORDER BY id DESC", (abierta["id"],)) if abierta else []
    ingresos = sum(m["monto"] for m in movs if m["tipo"] == "ingreso")
    egresos = sum(m["monto"] for m in movs if m["tipo"] == "egreso")
    esperado = (abierta["monto_apertura"] + ingresos - egresos) if abierta else 0
    historial = q("SELECT * FROM caja WHERE estado='cerrada' ORDER BY id DESC LIMIT 15")
    return page("Caja", """<h1>Caja</h1>
{% if abierta %}<p>Caja abierta por {{abierta.usuario_abre}} el {{abierta.fecha_apertura}} con ${{'{:,.0f}'.format(abierta.monto_apertura)}}</p>
<p>Ingresos: ${{'{:,.0f}'.format(ingresos)}} · Egresos: ${{'{:,.0f}'.format(egresos)}} · <b>Esperado en caja: ${{'{:,.0f}'.format(esperado)}}</b></p>
<form method=post class=row><input type=hidden name=accion value=mov><select name=tipo><option value=ingreso>Ingreso</option><option value=egreso>Egreso</option></select>
<input name=concepto placeholder=Concepto required><input name=monto type=number step=1000 placeholder=Monto required><button>Registrar movimiento</button></form>
<table><tr><th>Hora<th>Tipo<th>Concepto<th>Monto<th>Usuario</tr>{% for m in movs %}<tr><td>{{m.fecha[11:]}}<td>{{m.tipo}}<td>{{m.concepto}}<td>${{'{:,.0f}'.format(m.monto)}}<td>{{m.usuario}}</tr>{% endfor %}</table>
<h2>Cerrar caja</h2><form method=post action=/caja/cerrar class=row><input name=monto_cierre type=number step=1000 placeholder="Monto contado en caja" required><button>Cerrar caja</button></form>
{% else %}<form method=post class=row><input type=hidden name=accion value=abrir><input name=monto type=number step=1000 placeholder="Monto inicial" required><button>Abrir caja</button></form>{% endif %}
<h2>Historial de cierres</h2><table><tr><th>Apertura<th>Cierre<th>Monto inicial<th>Monto contado<th>Cerrada por</tr>
{% for c in historial %}<tr><td>{{c.fecha_apertura}}<td>{{c.fecha_cierre}}<td>${{'{:,.0f}'.format(c.monto_apertura)}}<td>${{'{:,.0f}'.format(c.monto_cierre or 0)}}<td>{{c.usuario_cierra}}</tr>{% endfor %}</table>""",
        abierta=abierta, movs=movs, ingresos=ingresos, egresos=egresos, esperado=esperado, historial=historial)

@app.post("/caja/cerrar")
@need("odontologo", "recepcion")
def caja_cerrar():
    abierta = q("SELECT * FROM caja WHERE estado='abierta' ORDER BY id DESC LIMIT 1", one=True)
    if abierta:
        ex("UPDATE caja SET estado='cerrada',fecha_cierre=?,monto_cierre=?,usuario_cierra=? WHERE id=?",
           (dt.datetime.now().isoformat(timespec="seconds"), float(request.form["monto_cierre"] or 0), session["u"], abierta["id"]))
        audit("cerrar_caja", f"id {abierta['id']} monto {request.form['monto_cierre']}")
    return redirect("/caja")

# ---- Reporte RIPS (Resolucion 948 de 2026) en formato JSON ----
TIPO_DOC_DEFECTO = "CC"

def build_rips_json(desde, hasta, atenciones_rows):
    """Arma el RIPS (transaccion + usuarios + servicios.consultas) segun estructura JSON vigente
    con la Resolucion 948 de 2026 (mantiene el esquema de la 2275 e incorpora codDiagnosticoPrincipalCie11
    en paralelo al CIE-10, campo exigido desde el 1 de julio de 2026)."""
    nit = q("SELECT value FROM settings WHERE key='fe_nit'", one=True)
    razon = q("SELECT value FROM settings WHERE key='fe_razon'", one=True)
    municipio = q("SELECT value FROM settings WHERE key='fe_municipio'", one=True)
    faltantes = []
    por_paciente = {}
    for a in atenciones_rows:
        p = q("SELECT * FROM patients WHERE id=?", (a["patient_id"],), one=True)
        if not p: continue
        if not p["documento"] or not p["fecha_nac"]:
            faltantes.append(p["nombre"])
        key = p["id"]
        if key not in por_paciente:
            por_paciente[key] = {
                "tipoDocumentoIdentificacion": p["tipo_doc"] or "CC",
                "numDocumentoIdentificacion": p["documento"] or "",
                "tipoUsuario": "10",
                "fechaNacimiento": p["fecha_nac"] or None,
                "codSexo": p["sexo"] or "F",
                "codPaisResidencia": "170",
                "codMunicipioResidencia": municipio["value"] if municipio else "41551",
                "codZonaTerritorialResidencia": "02",
                "incapacidad": "NO",
                "consecutivo": len(por_paciente) + 1,
                "servicios": {"consultas": []},
            }
        por_paciente[key]["servicios"]["consultas"].append({
            "codPrestador": nit["value"] if nit else "",
            "fechaInicioAtencion": f"{a['fecha']} {a['hora']}",
            "numAutorizacion": a["num_autorizacion"] or None,
            "codConsulta": a["cod_cups"],
            "modalidadGrupoServicioTecSal": a["modalidad"] or "01",
            "grupoServicios": "01",
            "codServicio": "328",
            "finalidadTecnologiaSalud": a["finalidad"] or "44",
            "causaMotivoAtencion": "38",
            "codDiagnosticoPrincipal": a["cod_cie10"],
            "codDiagnosticoPrincipalCie11": None,
            "tipoDiagnosticoPrincipal": a["tipo_diagnostico"] or "2",
            "viaIngresoUsuario": a["via_ingreso"] or "3",
            "vrServicio": a["valor"] or 0,
            "numFactura": a["num_factura"] or None,
            "descripcionCups": a["desc_cups"],
            "descripcionCie10": a["desc_cie10"],
        })
    return {
        "numDocumentoIdObligado": nit["value"] if nit else "",
        "razonSocial": razon["value"] if razon else "",
        "numFactura": None,
        "tipoNota": None,
        "periodoDesde": desde,
        "periodoHasta": hasta,
        "usuarios": list(por_paciente.values()),
    }, sorted(set(faltantes))

@app.route("/productos", methods=["GET", "POST"])
@need("admin", "recepcion", "odontologo")
def productos():
    if request.method == "POST":
        f = request.form
        if not f["name"].strip(): flash("El nombre es obligatorio.")
        else:
            ex("INSERT INTO products(name,kind,price,cost,stock,min,barcode,active) VALUES(?,?,?,?,?,?,?,1)",
               (f["name"].strip(), f.get("kind","insumo"), float(f.get("price") or 0), float(f.get("cost") or 0),
                int(f.get("stock") or 0), int(f.get("min") or 0), f.get("barcode","")))
            audit("crear_producto", f["name"].strip())
    ps = q("SELECT * FROM products WHERE active=1 ORDER BY name")
    n_bajo = sum(1 for p in ps if p["stock"] is not None and p["min"] is not None and p["stock"] <= p["min"])
    return page("Productos", """<h1>Productos e inventario</h1>
{% if n_bajo %}<p style="color:#dc2626;font-weight:600">&#9888; {{n_bajo}} producto(s) con stock bajo o agotado, revisa la lista resaltada en rojo.</p>{% endif %}
<table><tr><th>Nombre<th>Tipo<th>Precio<th>Costo<th>Stock<th>Mínimo</tr>
{% for p in ps %}<tr{% if p.stock is not none and p.min is not none and p.stock<=p.min %} style="background:#fee"{% endif %}>
<td>{{p.name}}<td>{{p.kind}}<td>${{'{:,.0f}'.format(p.price or 0)}}<td>${{'{:,.0f}'.format(p.cost or 0)}}<td>{{p.stock}}{% if p.stock is not none and p.min is not none and p.stock<=p.min %} <b style="color:#dc2626">⚠ Bajo</b>{% endif %}<td>{{p.min}}</tr>
{% else %}<tr><td colspan=6>Sin productos registrados.</tr>{% endfor %}</table>
<h2>Nuevo producto</h2><form method=post class=row>
<input name=name placeholder=Nombre required><select name=kind><option value=insumo>Insumo</option><option value=venta>Producto de venta</option><option value=servicio>Servicio</option></select>
<input name=price type=number step=100 placeholder=Precio><input name=cost type=number step=100 placeholder=Costo>
<input name=stock type=number placeholder=Stock><input name=min type=number placeholder="Stock mínimo"><input name=barcode placeholder="Código de barras">
<button>Guardar producto</button></form>""", ps=ps, n_bajo=n_bajo)

@app.route("/ventas", methods=["GET", "POST"])
@need("admin", "recepcion", "odontologo")
def ventas():
    if request.method == "POST":
        f = request.form
        pid = f.get("patient_id") or None
        prod_id = int(f["product_id"])
        qty = int(f.get("qty") or 1)
        prod = q("SELECT * FROM products WHERE id=?", (prod_id,), one=True)
        if not prod: flash("Producto no encontrado.")
        else:
            subtotal = (prod["price"] or 0) * qty
            total = subtotal
            now = dt.datetime.now().isoformat(timespec="seconds")
            items = f"{prod['name']} x{qty}"
            ex("""INSERT INTO sales(patient_id,user_id,date,subtotal,discount,tax,total,paid,method,items,voided)
                  VALUES(?,?,?,?,0,0,?,?,?,?,0)""",
               (pid, None, now, subtotal, total, total, f.get("method","efectivo"), items))
            ex("UPDATE products SET stock = stock - ? WHERE id=?", (qty, prod_id))
            audit("crear_venta", f"{items} total {total}")
    ventas_rows = q("SELECT s.*, p.nombre as paciente FROM sales s LEFT JOIN patients p ON p.id=s.patient_id WHERE s.voided=0 ORDER BY s.id DESC LIMIT 50")
    productos_disp = q("SELECT * FROM products WHERE active=1 ORDER BY name")
    pacientes_disp = q("SELECT * FROM patients ORDER BY nombre")
    return page("Ventas", """<h1>Ventas</h1>
<form method=post class=row>
<select name=patient_id><option value="">Venta sin paciente</option>{% for p in pacientes %}<option value="{{p.id}}">{{p.nombre}}</option>{% endfor %}</select>
<select name=product_id required>{% for pr in productos %}<option value="{{pr.id}}">{{pr.name}} (${{'{:,.0f}'.format(pr.price or 0)}})</option>{% endfor %}</select>
<input name=qty type=number value=1 min=1><select name=method><option value=efectivo>Efectivo</option><option value=tarjeta>Tarjeta</option><option value=transferencia>Transferencia</option></select>
<button>Registrar venta</button></form>
<h2>Últimas ventas</h2><table><tr><th>Fecha<th>Paciente<th>Detalle<th>Total<th>Método</tr>
{% for v in ventas %}<tr><td>{{v.date}}<td>{{v.paciente or '—'}}<td>{{v.items}}<td>${{'{:,.0f}'.format(v.total or 0)}}<td>{{v.method}}<td><a href="/einvoices/{{v.id}}">Factura DIAN</a></tr>
{% else %}<tr><td colspan=5>No hay ventas registradas.</tr>{% endfor %}</table>""", ventas=ventas_rows, productos=productos_disp, pacientes=pacientes_disp)

@app.route("/einvoices/<int:sale_id>")
@need("admin")
def einvoices(sale_id):
    venta = q("SELECT * FROM sales WHERE id=?", (sale_id,), one=True) or abort(404)
    fac = q("SELECT * FROM einvoices WHERE sale_id=?", (sale_id,), one=True)
    return page("Factura electrónica DIAN", """<h1>Factura electrónica (DIAN)</h1>
<p>Venta #{{venta.id}} — {{venta.date}} — Total: ${{'{:,.0f}'.format(venta.total or 0)}}</p>
{% if fac %}<p>Número: {{fac.number}}<br>CUFE: {{fac.cufe or 'pendiente'}}<br>Estado: {{fac.status}}</p>
{% else %}<p style=color:#b00>Esta venta todavía no tiene factura electrónica generada. La conexión con el facturador electrónico de la DIAN está pendiente de implementar en la versión web; por ahora esa función solo está disponible en el programa de escritorio.</p>{% endif %}""",
        venta=venta, fac=fac)

@app.route("/gastos", methods=["GET", "POST"])
@need("admin")
def gastos():
    if request.method == "POST":
        f = request.form
        ex("INSERT INTO expenses(date,concept,amount,user_id) VALUES(?,?,?,?)",
           (dt.date.today().isoformat(), f["concept"].strip(), float(f["amount"] or 0), None))
        audit("crear_gasto", f["concept"].strip())
    gs = q("SELECT * FROM expenses ORDER BY id DESC LIMIT 50")
    return page("Gastos", """<h1>Gastos</h1>
<form method=post class=row><input name=concept placeholder=Concepto required><input name=amount type=number step=100 placeholder=Valor required><button>Registrar gasto</button></form>
<table><tr><th>Fecha<th>Concepto<th>Valor</tr>{% for g in gs %}<tr><td>{{g.fecha if g.fecha else g.date}}<td>{{g.concept}}<td>${{'{:,.0f}'.format(g.amount or 0)}}</tr>{% endfor %}</table>""", gs=gs)

# ---- Odontograma grafico (dibujo en forma de arco de boca, dientes con forma real y condiciones clinicas) ----
DIENTES_ADULTO = [18,17,16,15,14,13,12,11,21,22,23,24,25,26,27,28,
                   48,47,46,45,44,43,42,41,31,32,33,34,35,36,37,38]
PUNTOS_DIENTES = {"18": [30.2, 246.7], "17": [37.0, 208.5], "16": [53.0, 172.2], "15": [77.5, 139.3], "14": [109.6, 111.2], "13": [147.9, 89.1], "12": [190.8, 73.8], "11": [236.6, 66.0], "21": [283.4, 66.0], "22": [329.2, 73.8], "23": [372.1, 89.1], "24": [410.4, 111.2], "25": [442.5, 139.3], "26": [467.0, 172.2], "27": [483.0, 208.5], "28": [489.8, 246.7], "48": [70.2, 271.5], "47": [75.8, 301.7], "46": [89.0, 330.4], "45": [109.3, 356.3], "44": [135.8, 378.5], "43": [167.4, 396.0], "42": [202.9, 408.1], "41": [240.7, 414.2], "31": [279.3, 414.2], "32": [317.1, 408.1], "33": [352.6, 396.0], "34": [384.2, 378.5], "35": [410.7, 356.3], "36": [431.0, 330.4], "37": [444.2, 301.7], "38": [449.8, 271.5]}

DIENTES_TEMPORAL = [55,54,53,52,51,61,62,63,64,65,85,84,83,82,81,71,72,73,74,75]
PUNTOS_DIENTES_TEMPORAL = {"55": [128.6, 143.7], "54": [151.7, 123.5], "53": [179.3, 107.6], "52": [210.2, 96.5], "51": [243.2, 90.9],
    "61": [276.8, 90.9], "62": [309.8, 96.5], "63": [340.7, 107.6], "64": [368.3, 123.5], "65": [391.4, 143.7],
    "85": [139.4, 351.0], "84": [160.6, 368.8], "83": [185.9, 382.8], "82": [214.3, 392.5], "81": [244.6, 397.4],
    "71": [275.4, 397.4], "72": [305.7, 392.5], "73": [334.1, 382.8], "74": [359.4, 368.8], "75": [380.6, 351.0]}
CONDICIONES = {
    "tratamiento": ("#fbbf24", "Pendiente por tratar"),
    "caries":      ("#e11d48", "Caries"),
    "obturado":    ("#2563eb", "Obturado / resina"),
    "corona":      ("#a855f7", "Corona"),
    "endodoncia":  ("#f59e0b", "Endodoncia"),
    "ausente":     ("#64748b", "Ausente / extraído"),
    "sano":        ("#16a34a", "Sano / realizado"),
}

@app.route("/pacientes/<int:pid>/odontograma", methods=["GET", "POST"])
@need("odontologo", "recepcion", "radiologo", "admin")
def odontograma(pid):
    p = q("SELECT * FROM patients WHERE id=?", (pid,), one=True) or abort(404)
    vista = request.args.get("vista", "adulto")
    if vista not in ("adulto", "temporal"): vista = "adulto"
    if request.method == "POST" and session["rol"] in ("odontologo", "admin"):
        f = request.form
        ex("""INSERT INTO plan(patient_id,diente,tratamiento,etapa,costo,estado,notas,condicion,profesional,fecha_registro)
              VALUES(?,?,?,?,?,?,?,?,?,?)""",
           (pid, f["diente"], f["tratamiento"].strip(), "Odontograma", float(f.get("costo") or 0), "pendiente",
            f.get("notas", ""), f.get("condicion", "tratamiento"), session.get("u"), dt.date.today().isoformat()))
        audit("odontograma_marca", f"paciente {pid} diente {f['diente']} ({f.get('condicion','tratamiento')})")
        return redirect(f"/pacientes/{pid}/odontograma?vista={f.get('vista_actual','adulto')}")
    marcas = q("SELECT * FROM plan WHERE patient_id=? ORDER BY id DESC", (pid,))
    ultima_por_diente = {}
    for m in marcas:
        if str(m["diente"]) not in ultima_por_diente:
            ultima_por_diente[str(m["diente"])] = m
    lista_dientes = DIENTES_ADULTO if vista == "adulto" else DIENTES_TEMPORAL
    puntos = PUNTOS_DIENTES if vista == "adulto" else PUNTOS_DIENTES_TEMPORAL
    dientes_svg = ""
    for d in lista_dientes:
        x, y = puntos[str(d)]
        m = ultima_por_diente.get(str(d))
        cond = (m["condicion"] if m and m["condicion"] else None)
        relleno, _ = CONDICIONES.get(cond, ("#ffffff", ""))
        borde = "#1e3a8a" if not m else "#334155"
        dientes_svg += (
            f'<g class="diente" onclick="document.getElementById(\'diente\').value=\'{d}\';'
            f'document.getElementById(\'diente\').scrollIntoView({{behavior:\'smooth\'}});" style="cursor:pointer">'
            f'<path d="M {x-15} {y-4} Q {x-16} {y-16} {x} {y-16} Q {x+16} {y-16} {x+15} {y-4} '
            f'Q {x+15} {y+10} {x+9} {y+14} Q {x} {y+18} {x-9} {y+14} Q {x-15} {y+10} {x-15} {y-4} Z" '
            f'fill="{relleno}" stroke="{borde}" stroke-width="1.6"/>'
            f'<text x="{x}" y="{y+1}" text-anchor="middle" font-size="10.5" font-family="Arial" '
            f'font-weight="600" fill="{"#fff" if cond and cond!="tratamiento" else "#1e3a8a"}">{d}</text>'
            f'</g>'
        )
    svg = (
        '<svg viewBox="0 0 520 450" style="width:100%;max-width:600px;height:auto;background:linear-gradient(180deg,#fdf2f8,#f8fafc);'
        'border:1px solid var(--line);border-radius:16px;padding:10px;box-shadow:0 1px 4px rgba(15,23,42,.08)">'
        '<ellipse cx="260" cy="240" rx="258" ry="218" fill="none" stroke="#f3a9c7" stroke-width="2" stroke-dasharray="3 5"/>'
        '<line x1="260" y1="30" x2="260" y2="450" stroke="#e2e8f0" stroke-width="1" stroke-dasharray="2 4"/>'
        '<text x="260" y="18" text-anchor="middle" font-size="11" fill="#94a3b8" font-family="Arial">Arcada superior</text>'
        '<text x="260" y="445" text-anchor="middle" font-size="11" fill="#94a3b8" font-family="Arial">Arcada inferior</text>'
        + dientes_svg + '</svg>'
    )
    leyenda = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:5px;margin:3px 10px 3px 0;font-size:12.5px;color:var(--mute)">'
        f'<span style="width:12px;height:12px;background:{color};border-radius:3px;display:inline-block"></span>{label}</span>'
        for color, label in CONDICIONES.values()
    )
    opciones_select = "".join(f'<option value="{k}">{v[1]}</option>' for k, v in CONDICIONES.items())
    selector_vista = (
        f'<div style="margin-bottom:10px"><a class="btn" '
        f'href="/pacientes/{pid}/odontograma?vista=adulto" style="{"opacity:1" if vista=="adulto" else "opacity:.55"};margin-right:8px">Dentición permanente</a>'
        f'<a class="btn" href="/pacientes/{pid}/odontograma?vista=temporal" style="{"opacity:1" if vista=="temporal" else "opacity:.55"}">Dentición temporal (niños)</a></div>'
    )
    return page(f"Odontograma - {p['nombre']}", """<h1>Odontograma de {{p.nombre}}</h1>
<p><a href="/pacientes/{{p.id}}">&larr; Volver a la ficha del paciente</a></p>""" + selector_vista + """
<div class=card style="display:flex;flex-direction:column;align-items:center">""" + svg + """
<div style="margin-top:10px;text-align:center">""" + leyenda + """</div>
<p style="font-size:13px;color:var(--mute)">Haz clic sobre un diente para seleccionarlo abajo. Si un diente tiene varios registros, se muestra el más reciente.</p>
</div>
{% if session.rol in ['odontologo','admin'] %}<div class=card><form method=post class=row>
<input type=hidden name=vista_actual value="""" + vista + """">
<input id=diente name=diente placeholder="N° diente" required size=4>
<select name=condicion>""" + opciones_select + """</select>
<input name=tratamiento placeholder=Tratamiento required size=20><input name=costo type=number step=1000 placeholder=Costo>
<input name=notas placeholder=Notas size=20><button>Registrar en el odontograma</button></form></div>{% endif %}
<h2>Historial por diente</h2>
<table><tr><th>Diente<th>Condición<th>Tratamiento<th>Estado<th>Costo<th>Profesional<th>Fecha</tr>
{% for m in marcas %}<tr><td>{{m.diente}}<td>{{m.condicion or 'tratamiento'}}<td>{{m.tratamiento}}<td>{{m.estado}}<td>${{'{:,.0f}'.format(m.costo or 0)}}<td>{{m.profesional or '—'}}<td>{{m.fecha_registro or '—'}}</tr>
{% else %}<tr><td colspan=7>Todavía no hay nada registrado en el odontograma.</tr>{% endfor %}</table>""",
        p=p, marcas=marcas)

# ---- Odontologos ----
@app.route("/odontologos", methods=["GET", "POST"])
@need("admin")
def odontologos():
    if request.method == "POST":
        f = request.form
        ex("INSERT INTO dentists(name,specialty,license,phone,active) VALUES(?,?,?,?,1)",
           (f["name"].strip(), f.get("specialty", ""), f.get("license", ""), f.get("phone", "")))
        audit("crear_odontologo", f["name"].strip())
    ds = q("SELECT * FROM dentists WHERE active=1 ORDER BY name")
    return page("Odontólogos", """<h1>Odontólogos</h1>
<table><tr><th>Nombre<th>Especialidad<th>Tarjeta profesional<th>Teléfono</tr>
{% for d in ds %}<tr><td>{{d.name}}<td>{{d.specialty}}<td>{{d.license}}<td>{{d.phone}}</tr>{% else %}<tr><td colspan=4>Sin odontólogos registrados.</tr>{% endfor %}</table>
<h2>Nuevo odontólogo</h2><form method=post class=row><input name=name placeholder=Nombre required>
<input name=specialty placeholder=Especialidad><input name=license placeholder="Tarjeta profesional / RETHUS"><input name=phone placeholder=Teléfono><button>Guardar</button></form>""", ds=ds)

# ---- Catalogo de servicios ----
@app.route("/servicios", methods=["GET", "POST"])
@need("admin")
def servicios():
    if request.method == "POST":
        f = request.form
        ex("INSERT INTO services(name,category,price,active) VALUES(?,?,?,1)",
           (f["name"].strip(), f.get("category", ""), float(f.get("price") or 0)))
        audit("crear_servicio", f["name"].strip())
    ss = q("SELECT * FROM services WHERE active=1 ORDER BY category, name")
    return page("Catálogo de servicios", """<h1>Catálogo de servicios</h1>
<table><tr><th>Servicio<th>Categoría<th>Precio</tr>{% for s in ss %}<tr><td>{{s.name}}<td>{{s.category}}<td>${{'{:,.0f}'.format(s.price or 0)}}</tr>
{% else %}<tr><td colspan=3>Sin servicios registrados.</tr>{% endfor %}</table>
<h2>Nuevo servicio</h2><form method=post class=row><input name=name placeholder=Nombre required><input name=category placeholder=Categoría><input name=price type=number step=1000 placeholder=Precio><button>Guardar</button></form>""", ss=ss)

@app.route("/configuracion/respaldo")
@need()
def configuracion_respaldo():
    if session["rol"] != "admin": abort(403)
    import json as _json
    tablas = ["patients","citas","plan","labs","recetas","receta_items","caja","caja_mov",
              "atenciones","products","sales","expenses","purchases","dentists","services",
              "notes","quotes","abonos","consentimientos","cuentas","settings"]
    datos = {}
    for t in tablas:
        try:
            filas = q(f"SELECT * FROM {t}")
            datos[t] = [dict(f) for f in filas]
        except Exception as e:
            datos[t] = {"error": str(e)}
    contenido = _json.dumps(datos, ensure_ascii=False, indent=2, default=str)
    audit("descargar_respaldo", f"{len(tablas)} tablas")
    nombre = f"respaldo_consultorio_{dt.date.today().isoformat()}.json"
    r = app.response_class(contenido, mimetype="application/json")
    r.headers["Content-Disposition"] = f"attachment; filename={nombre}"
    return r

@app.route("/configuracion", methods=["GET", "POST"])
@need()
def configuracion():
    if session["rol"] != "admin": abort(403)
    campos = [("fe_nit", "NIT del prestador (sin dígito de verificación)"), ("fe_razon", "Razón social"), ("fe_municipio", "Código de municipio DANE")]
    if request.method == "POST":
        for k, _ in campos:
            ex("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, request.form.get(k, "")))
        logo = request.files.get("logo")
        if logo and logo.filename:
            ext = os.path.splitext(logo.filename)[1].lower()
            tipos = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml", ".webp": "image/webp"}
            if ext in tipos:
                nombre_logo = "logo" + ext
                ok_guardado = storage_guardar(nombre_logo, logo.read(), tipos[ext])
                if ok_guardado:
                    ex("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", ("logo_file", nombre_logo))
                    flash("Logo actualizado.")
                else:
                    flash("No se pudo guardar el logo. Intenta de nuevo.")
            else:
                flash("Formato de logo no válido. Usa PNG, JPG, SVG o WEBP.")
        audit("configuracion", "actualizo datos del prestador")
        flash("Configuración guardada.")
    vals = {k: (q("SELECT value FROM settings WHERE key=?", (k,), one=True) or {"value": ""})["value"] for k, _ in campos}
    logo_actual = (q("SELECT value FROM settings WHERE key='logo_file'", one=True) or {"value": ""})["value"]
    smtp_keys = ["smtp_host", "smtp_port", "smtp_user", "smtp_pass", "smtp_from_name"]
    smtp_vals = {k: (q("SELECT value FROM settings WHERE key=?", (k,), one=True) or {"value": ""})["value"] for k in smtp_keys}
    return page("Configuración", """<h1>Configuración del prestador</h1>
<form method=post enctype=multipart/form-data class=row style=flex-direction:column;max-width:420px>
{% for k,label in campos %}<label>{{label}}<br><input name="{{k}}" value="{{vals[k]}}" style=width:100%></label>{% endfor %}
<label>Logo del consultorio (aparece en el menú lateral)<br>
{% if logo_actual %}<div style="margin:8px 0"><img src="/logo?v={{logo_actual}}" style="max-height:60px;max-width:160px;border-radius:8px;background:#fff;padding:4px"></div>{% endif %}
<input type=file name=logo accept="image/*"></label>
<button>Guardar</button></form>
<h2>Correo para recordatorios de citas</h2>
<p style="font-size:13.5px;color:var(--mute)">Configura aquí el correo que enviará los recordatorios a tus pacientes. Con Gmail usa
servidor smtp.gmail.com, puerto 587, tu correo de Gmail como usuario, y en contraseña NO uses tu clave normal: genera una
"contraseña de aplicación" en la configuración de seguridad de tu cuenta de Google.</p>
{% if email_activo %}<p style="color:var(--bien);font-weight:600">&#9679; Correo configurado y activo.</p>
{% else %}<p style="color:var(--adv);font-weight:600">&#9679; Correo todavía no configurado.</p>{% endif %}
<form method=post action="/configuracion/correo" class=row style=flex-direction:column;max-width:420px>
<label>Servidor SMTP<br><input name=smtp_host value="{{smtp_vals.smtp_host}}" placeholder="smtp.gmail.com" style=width:100%></label>
<label>Puerto<br><input name=smtp_port value="{{smtp_vals.smtp_port or 587}}" style=width:100%></label>
<label>Correo (usuario)<br><input name=smtp_user value="{{smtp_vals.smtp_user}}" placeholder="tuconsultorio@gmail.com" style=width:100%></label>
<label>Contraseña de aplicación<br><input type=password name=smtp_pass value="{{smtp_vals.smtp_pass}}" style=width:100%></label>
<label>Nombre que verá el paciente<br><input name=smtp_from_name value="{{smtp_vals.smtp_from_name}}" style=width:100%></label>
<button>Guardar correo</button></form>
<h2>Respaldo de la información</h2>
<p style="font-size:13.5px;color:var(--mute)">Descarga una copia completa de pacientes, citas, ventas, gastos y demás datos
del consultorio en un archivo. Guárdalo en un lugar seguro (tu computador o una nube) de vez en cuando, por si algo falla.</p>
<a href="/configuracion/respaldo"><button type=button onclick="this.closest('a').style.pointerEvents='none'">Descargar respaldo ahora</button></a>
""", campos=campos, vals=vals, logo_actual=logo_actual,
        email_activo=email_listo(), smtp_vals=smtp_vals)

@app.post("/configuracion/correo")
@need()
def configuracion_correo():
    if session["rol"] != "admin": abort(403)
    f = request.form
    for k in ["smtp_host", "smtp_port", "smtp_user", "smtp_pass", "smtp_from_name"]:
        ex("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, f.get(k, "").strip()))
    audit("configuracion_correo", "actualizo datos SMTP de recordatorios")
    flash("Datos de correo guardados.")
    return redirect("/configuracion")

@app.route("/rips", methods=["GET", "POST"])
@need("odontologo", "admin")
def rips():
    hoy = dt.date.today()
    desde = request.values.get("desde", hoy.replace(day=1).isoformat())
    hasta = request.values.get("hasta", hoy.isoformat())
    filas = q("""SELECT a.*, p.nombre FROM atenciones a JOIN patients p ON p.id=a.patient_id
                 WHERE a.fecha BETWEEN ? AND ? ORDER BY a.fecha""", (desde, hasta))
    archivo_generado = None
    faltantes = []
    if request.method == "POST":
        import json as _json
        data, faltantes = build_rips_json(desde, hasta, filas)
        if not faltantes:
            nombre = f"RIPS_{desde}_{hasta}.json"
            ruta = os.path.join(BASE, "rips_exportados")
            os.makedirs(ruta, exist_ok=True)
            with open(os.path.join(ruta, nombre), "w", encoding="utf-8") as fh:
                _json.dump(data, fh, ensure_ascii=False, indent=2)
            ex("INSERT INTO rips_lotes(fecha,desde,hasta,archivo,n_usuarios,n_registros,usuario) VALUES(?,?,?,?,?,?,?)",
               (dt.datetime.now().isoformat(timespec="seconds"), desde, hasta, nombre, len(data["usuarios"]), len(filas), session["u"]))
            audit("generar_rips", f"{desde} a {hasta}: {len(filas)} registros")
            archivo_generado = nombre
    lotes = q("SELECT * FROM rips_lotes ORDER BY id DESC LIMIT 15")
    reps_listo = bool(os.environ.get("REPS_API_URL") and os.environ.get("REPS_TOKEN"))
    return page("Reporte RIPS", """<h1>Reporte RIPS para el Ministerio de Salud</h1>
<p>Genera el archivo JSON con las atenciones del periodo, codificadas en CUPS y CIE-10, según la Resolución 948 de 2026.</p>
<form method=post class=row><input type=date name=desde value="{{desde}}"><input type=date name=hasta value="{{hasta}}"><button>Generar RIPS del periodo</button></form>
{% if faltantes %}<div class=msg style=color:#b00>No se generó el archivo. Estos pacientes no tienen documento o fecha de nacimiento completos: {{faltantes|join(', ')}}. Edítalos en su ficha antes de generar el RIPS.</div>{% endif %}
{% if archivo %}<div class=msg>Archivo generado: {{archivo}}. <a href="/rips/descargar/{{archivo}}">Descargarlo</a>
{% if reps_listo %} · <form method=post action="/rips/enviar/{{archivo}}" style="display:inline"><button style="background:var(--bien)">Enviar al Ministerio</button></form>
{% else %} · <span style="color:var(--mute)">Enviar al Ministerio (requiere credenciales REPS, ver Configuración)</span>{% endif %}</div>{% endif %}
<h2>Atenciones en el periodo ({{filas|length}})</h2><table><tr><th>Fecha<th>Paciente<th>CUPS<th>CIE-10<th>Valor</tr>
{% for a in filas %}<tr><td>{{a.fecha}}<td>{{a.nombre}}<td>{{a.cod_cups}}<td>{{a.cod_cie10}}<td>${{'{:,.0f}'.format(a.valor or 0)}}</tr>
{% else %}<tr><td colspan=5>No hay atenciones registradas en este periodo.</tr>{% endfor %}</table>
<h2>Reportes generados antes</h2><table><tr><th>Fecha<th>Periodo<th>Usuarios<th>Registros<th></tr>
{% for l in lotes %}<tr><td>{{l.fecha}}<td>{{l.desde}} a {{l.hasta}}<td>{{l.n_usuarios}}<td>{{l.n_registros}}<td><a href="/rips/descargar/{{l.archivo}}">Descargar</a>
{% if reps_listo %} · <form method=post action="/rips/enviar/{{l.archivo}}" style="display:inline"><button style="background:var(--bien);padding:4px 10px;font-size:12.5px">Enviar</button></form>{% endif %}</tr>{% endfor %}</table>
{% if not reps_listo %}<div class=card style="font-size:13.5px;color:var(--mute)"><b>Envío automático al Ministerio:</b> todavía no está activo. Cuando tengas tu registro y credenciales como prestador (REPS), se configuran en Render como variables <code>REPS_API_URL</code> y <code>REPS_TOKEN</code>, y el botón "Enviar al Ministerio" aparecerá automáticamente aquí, sin tocar más código.</div>{% endif %}""",
        desde=desde, hasta=hasta, filas=filas, archivo=archivo_generado, lotes=lotes, faltantes=faltantes, reps_listo=reps_listo)

@app.post("/rips/enviar/<path:nombre>")
@need("odontologo", "admin")
def rips_enviar(nombre):
    api_url = os.environ.get("REPS_API_URL")
    token = os.environ.get("REPS_TOKEN")
    if not (api_url and token):
        flash("El envío automático todavía no está configurado. Agrega REPS_API_URL y REPS_TOKEN en Render cuando tengas tus credenciales del Ministerio.")
        return redirect("/rips")
    ruta = os.path.join(BASE, "rips_exportados", nombre)
    if not os.path.exists(ruta):
        flash("No se encontró ese archivo RIPS.")
        return redirect("/rips")
    try:
        with open(ruta, "rb") as fh:
            contenido = fh.read()
        req = urlreq.Request(api_url, data=contenido, method="POST",
                              headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        with urlreq.urlopen(req, timeout=30) as resp:
            codigo = resp.status
        audit("enviar_rips_ministerio", f"{nombre}: respuesta {codigo}")
        flash(f"Archivo {nombre} enviado al Ministerio (respuesta {codigo}).")
    except Exception as e:
        audit("error_envio_rips", f"{nombre}: {e}")
        flash(f"No se pudo enviar el archivo: {e}")
    return redirect("/rips")

@app.route("/rips/descargar/<path:n>")
@need("odontologo", "admin")
def rips_descargar(n):
    return send_from_directory(os.path.join(BASE, "rips_exportados"), n, as_attachment=True)

@app.route("/usuarios", methods=["GET", "POST"])
@need()
def usuarios():
    if session["rol"] != "admin": abort(403)
    if request.method == "POST":
        f = request.form
        if not clave_segura(f["clave"]):
            flash("La contraseña debe tener al menos 8 caracteres, con letras y números.")
        else:
            try:
                ex("INSERT INTO users(usuario,nombre,rol,clave) VALUES(?,?,?,?)", (f["usuario"].strip(), f["nombre"], f["rol"], generate_password_hash(f["clave"])))
                audit("crear_usuario", f["usuario"].strip())
            except sqlite3.IntegrityError: flash("Ese usuario ya existe.")
    return page("Usuarios", """<h1>Usuarios</h1><table><tr><th>Usuario<th>Nombre<th>Rol<th></tr>{% for u in us %}<tr><td>{{u.usuario}}<td>{{u.nombre}}<td>{{u.rol}}
<td>{% if u.usuario != session.u %}<form method=post action="/usuarios/{{u.id}}/eliminar" onsubmit="return confirm('¿Eliminar al usuario {{u.usuario}}? Esta acción no se puede deshacer.')" style="margin:0">
<button style="background:var(--mal);padding:5px 10px;font-size:12.5px">Eliminar</button></form>{% else %}<span style="color:var(--mute);font-size:12.5px">Tu usuario actual</span>{% endif %}</tr>{% endfor %}</table>
<h2>Nuevo usuario</h2><form method=post class=row><input name=usuario placeholder=Usuario required><input name=nombre placeholder=Nombre>
<input name=clave type=password placeholder="Contraseña (mínimo 8, letras y números)" required><select name=rol>{% for r in roles %}<option>{{r}}</option>{% endfor %}</select>
<button>Crear usuario</button></form><h2>Auditoría reciente</h2><table><tr><th>Fecha<th>Usuario<th>Acción<th>Detalle</tr>
{% for a in aud %}<tr><td>{{a.fecha}}<td>{{a.usuario}}<td>{{a.accion}}<td>{{a.detalle}}</tr>{% endfor %}</table>""",
        us=q("SELECT * FROM users"), roles=ROLES, aud=q("SELECT * FROM auditoria ORDER BY id DESC LIMIT 50"))

@app.post("/usuarios/<int:uid>/eliminar")
@need()
def usuario_eliminar(uid):
    if session["rol"] != "admin": abort(403)
    objetivo = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if not objetivo:
        flash("Ese usuario no existe.")
        return redirect("/usuarios")
    if objetivo["usuario"] == session["u"]:
        flash("No puedes eliminar tu propio usuario mientras tienes la sesión abierta.")
        return redirect("/usuarios")
    ex("DELETE FROM users WHERE id=?", (uid,))
    audit("eliminar_usuario", objetivo["usuario"])
    flash(f"Usuario {objetivo['usuario']} eliminado.")
    return redirect("/usuarios")

init()
if not USE_PG:
    backup_db()
else:
    print("Consultorio web: usando base de datos PostgreSQL externa (Supabase).")
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

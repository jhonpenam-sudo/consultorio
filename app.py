"""Consultorio web - Dra. Natalia Morera. Ejecutar: pip install flask && python app.py
Variables: SECRET_KEY, ADMIN_PASSWORD, DB_PATH, UPLOAD_DIR"""
import os, sqlite3, datetime as dt, urllib.parse as up, time, shutil, re
from functools import wraps
from flask import (Flask, g, request, redirect, session, render_template_string,
                   flash, abort, send_from_directory)
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
"""

def _pg_sql(sql):
    # Traduce SQLite -> PostgreSQL: placeholders, tipos, e INSERT OR REPLACE
    s = sql
    s = s.replace("INSERT OR REPLACE INTO", "__UPSERT__")
    s = re.sub(r"\?", "%s", s)
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
        if s.strip().upper().startswith("INSERT") and "RETURNING" not in s.upper():
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
nav{width:240px;background:linear-gradient(180deg,#1e3a8a,#172e68);color:#fff;padding:22px 14px;flex:none;box-shadow:2px 0 10px rgba(0,0,0,.08)}
nav b{display:block;font-size:17px;margin-bottom:4px;color:#fff;letter-spacing:.02em}
nav small{display:block;color:#aecbf5;margin-bottom:20px;font-size:11px}
nav .grp{color:#8fb8da;font-size:11px;text-transform:uppercase;letter-spacing:.07em;margin:16px 6px 6px;font-weight:600}
nav a{display:block;color:#eaf2fa;text-decoration:none;padding:9px 12px;border-radius:8px;font-size:14px;margin-bottom:2px;transition:background .15s}
nav a:hover{background:rgba(255,255,255,.14)}
main{flex:1;padding:30px 34px;max-width:1100px;overflow-x:auto}
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
.card{background:var(--card);border-radius:14px;padding:18px 20px;box-shadow:0 1px 4px rgba(15,23,42,.08);margin:14px 0}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:20px}
.brand-badge{width:42px;height:42px;border-radius:50%;background:linear-gradient(135deg,#60a5fa,var(--acento));color:#fff;
  display:flex;align-items:center;justify-content:center;font-family:Georgia,"Times New Roman",serif;font-weight:700;font-size:16px;flex:none;box-shadow:0 2px 6px rgba(0,0,0,.25)}
.brand-logo{width:42px;height:42px;border-radius:50%;object-fit:cover;background:#fff;flex:none;box-shadow:0 2px 6px rgba(0,0,0,.25)}
nav .brand b{font-family:Georgia,"Times New Roman",serif;font-size:18px;letter-spacing:.01em}
.stats{display:flex;gap:14px;flex-wrap:wrap;margin:14px 0 22px}
.stat{flex:1;min-width:150px;background:var(--card);border-radius:14px;padding:16px 18px;box-shadow:0 1px 4px rgba(15,23,42,.08);border-left:5px solid var(--azul)}
.stat.v2{border-left-color:var(--acento)}.stat.v3{border-left-color:var(--adv)}.stat.v4{border-left-color:#8b5cf6}
.stat b{display:block;font-size:28px;color:var(--azul-osc);line-height:1.1}
.stat span{display:block;font-size:12.5px;color:var(--mute);margin-top:4px;text-transform:uppercase;letter-spacing:.03em}
.citas-hoy{display:flex;flex-direction:column;gap:10px}
.cita-card{display:flex;align-items:center;gap:14px;background:var(--card);border-radius:12px;padding:12px 16px;box-shadow:0 1px 3px rgba(15,23,42,.08);border-left:4px solid var(--azul)}
.cita-card .hora{font-weight:700;color:var(--azul-osc);min-width:56px;font-size:15px}
.cita-card .info{flex:1}
.cita-card .info b{display:block;font-size:14.5px}
.cita-card .info small{color:var(--mute)}
.pill{display:inline-block;padding:3px 10px;border-radius:99px;font-size:11.5px;font-weight:600;text-transform:uppercase;letter-spacing:.02em}
.pill.pendiente{background:#fff3cd;color:#92680a}.pill.confirmada{background:#dcfce7;color:#15803d}
.pill.completada{background:#dbeafe;color:#1d4ed8}.pill.cancelada{background:#fee2e2;color:#b91c1c}
@media(max-width:700px){body{display:block}nav{width:auto}main{padding:16px}}
@media print{nav,.noprint{display:none}}
</style></head><body>
{% if session.u %}<nav><div class=brand>{% if logo_file %}<img src="/logo?v={{logo_file}}" class=brand-logo>{% else %}<span class=brand-badge>NM</span>{% endif %}<div><b>Dra. Natalia Morera</b><small>Odontología Especializada</small></div></div>
<div class=grp>Panel</div><a href=/>Inicio</a>
<div class=grp>Pacientes</div><a href=/pacientes>Pacientes</a><a href=/citas>Citas / Agenda</a><a href=/recordatorios>Recordatorios</a><a href=/reactivacion>Reactivar pacientes</a>
<div class=grp>Catálogos</div><a href=/odontologos>Odontólogos</a><a href=/servicios>Servicios</a>
<div class=grp>Punto de venta</div><a href=/ventas>Punto de venta</a><a href=/productos>Inventario</a><a href=/caja>Caja</a><a href=/gastos>Gastos</a>
<div class=grp>Reportes y cumplimiento</div><a href=/rips>Reporte RIPS</a>
<div class=grp>Facturación</div><a href=/ventas>Facturación electrónica</a>
{% if session.rol=='admin' %}<div class=grp>Administración</div><a href=/usuarios>Usuarios y personal</a><a href=/configuracion>Ajustes</a>{% endif %}
<div class=grp>Portal</div><a href=/reservar>Portal de reservas</a>
<a href=/logout style="margin-top:14px;color:#ffd3d3">Cerrar sesión ({{session.u}})</a></nav>{% endif %}
<main>{% for m in get_flashed_messages() %}<div class=msg>{{m}}</div>{% endfor %}%%BODY%%</main></body></html>"""

def page(t, body, **c):
    logo_file = ""
    if session.get("u"):
        row = q("SELECT value FROM settings WHERE key='logo_file'", one=True)
        logo_file = row["value"] if row else ""
    return render_template_string(LAYOUT.replace("%%BODY%%", body), t=t, logo_file=logo_file, **c)

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
    return page("Inicio", """<h1>Hoy, {{hoy_legible}}</h1>
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

@app.route("/pacientes", methods=["GET", "POST"])
@need("odontologo", "recepcion", "radiologo")
def pacientes():
    if request.method == "POST":
        f = request.form
        if not f["nombre"].strip(): flash("El nombre es obligatorio.")
        else:
            pid = ex("INSERT INTO patients(nombre,documento,tipo_doc,fecha_nac,sexo,telefono,email,creado) VALUES(?,?,?,?,?,?,?,?)",
                     (f["nombre"].strip(), f["documento"], f.get("tipo_doc","CC"), f.get("fecha_nac") or None, f.get("sexo","F"), f["telefono"], f["email"], dt.date.today().isoformat()))
            audit("crear_paciente", f["nombre"].strip())
            return redirect(f"/pacientes/{pid}")
    s = "%" + request.args.get("s", "") + "%"
    ps = q("SELECT * FROM patients WHERE nombre LIKE ? OR documento LIKE ? ORDER BY nombre", (s, s))
    return page("Pacientes", """<h1>Pacientes</h1><form class=row><input name=s placeholder="Buscar nombre o documento" value="{{request.args.s}}"><button>Buscar</button></form>
<table><tr><th>Nombre<th>Documento<th>Teléfono</tr>{% for p in ps %}<tr><td><a href=/pacientes/{{p.id}}>{{p.nombre}}</a><td>{{p.documento}}<td>{{p.telefono}}</tr>{% endfor %}</table>
<h2>Nuevo paciente</h2><form method=post class=row><input name=nombre placeholder=Nombre required>
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
    return page(p["nombre"], """<h1>{{p.nombre}}</h1><p>{{p.documento}} · {{p.telefono}} · {{p.email}}</p>
{% if p.alergias %}<div class=msg style="background:#fee2e2;border-left-color:var(--mal)"><b>⚠ Alergias:</b> {{p.alergias}}</div>{% endif %}
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
<h2>Radiografías</h2><table><tr><th>Fecha<th>Descripción<th>Subida por<th></tr>{% for x in rx %}<tr><td>{{x.fecha}}<td>{{x.descripcion}}<td>{{x.subido_por}}<td><a href=/rx/{{x.archivo}}>Descargar</a></tr>{% endfor %}</table>
<form method=post action=/pacientes/{{p.id}}/rx enctype=multipart/form-data class=row><input type=file name=archivo required><input name=descripcion placeholder=Descripción><button>Subir radiografía</button></form>
<div class=card style="text-align:center;background:linear-gradient(135deg,#2563eb,#1e3a8a)">
<a href="/pacientes/{{p.id}}/odontograma" style="color:#fff;text-decoration:none;font-size:18px;font-weight:700">🦷 Ver odontograma gráfico</a>
</div>
<h2>Notas clínicas</h2><table><tr><th>Fecha<th>Usuario<th>Nota</tr>{% for n in notas %}<tr><td>{{n.fecha}}<td>{{n.usuario}}<td>{{n.texto}}</tr>{% else %}<tr><td colspan=3>Sin notas.</tr>{% endfor %}</table>
{% if session.rol in ['odontologo','admin'] %}<form method=post action="/pacientes/{{p.id}}/notas" class=row><input name=texto placeholder="Escribir nota clínica" style="flex:1" required><button>Agregar nota</button></form>{% endif %}
<h2>Cotizaciones</h2><table><tr><th>Fecha<th>Detalle<th>Total</tr>{% for c in cotizaciones %}<tr><td>{{c.fecha}}<td>{{c.items}}<td>${{'{:,.0f}'.format(c.total or 0)}}</tr>{% else %}<tr><td colspan=3>Sin cotizaciones.</tr>{% endfor %}</table>
{% if session.rol in ['odontologo','admin','recepcion'] %}<form method=post action="/pacientes/{{p.id}}/cotizacion" class=row><input name=items placeholder="Detalle del tratamiento cotizado" style="flex:1" required><input name=total type=number step=1000 placeholder=Total required><button>Guardar cotización</button></form>{% endif %}
<h2>Abonos y cartera</h2><table><tr><th>Fecha<th>Concepto<th>Monto</tr>{% for a in abonos %}<tr><td>{{a.fecha}}<td>{{a.concepto}}<td>${{'{:,.0f}'.format(a.monto or 0)}}</tr>{% else %}<tr><td colspan=3>Sin abonos registrados.</tr>{% endfor %}</table>
<p>Total abonado: ${{'{:,.0f}'.format(total_abonos)}}</p>
{% if session.rol in ['odontologo','admin','recepcion'] %}<form method=post action="/pacientes/{{p.id}}/abono" class=row><input name=concepto placeholder=Concepto required><input name=monto type=number step=1000 placeholder=Monto required><button>Registrar abono</button></form>{% endif %}
<h2>Atenciones (reporte RIPS)</h2><table><tr><th>Fecha<th>CUPS<th>CIE-10<th>Valor<th>Usuario</tr>{% for a in atenciones %}<tr><td>{{a.fecha}}<td>{{a.cod_cups}} {{a.desc_cups}}<td>{{a.cod_cie10}} {{a.desc_cie10}}<td>${{'{:,.0f}'.format(a.valor or 0)}}<td>{{a.usuario}}</tr>{% endfor %}</table>
{% if session.rol in ['odontologo','admin'] %}<form method=post action=/pacientes/{{p.id}}/atencion class=row>
<input name=cod_cups placeholder="Código CUPS" required size=8><input name=desc_cups placeholder="Procedimiento" required size=22>
<input name=cod_cie10 placeholder="Código CIE-10" required size=8><input name=desc_cie10 placeholder="Diagnóstico" required size=22>
<select name=tipo_diagnostico><option value=1>Impresión diagnóstica</option><option value=2>Confirmado nuevo</option><option value=3>Confirmado repetido</option></select>
<input name=valor type=number step=1000 placeholder=Valor required><input name=num_factura placeholder="N° factura" size=10><button>Registrar atención</button></form>{% endif %}""",
        p=p, plan=plan, plan_g=plan_g, tot=tot, hecho=hecho,
        labs=q("SELECT * FROM labs WHERE patient_id=? ORDER BY id DESC", (pid,)),
        recetas=q("""SELECT r.*, (SELECT GROUP_CONCAT(medicamento,', ') FROM receta_items WHERE receta_id=r.id) resumen
                      FROM recetas r WHERE patient_id=? ORDER BY id DESC""", (pid,)),
        rx=q("SELECT * FROM rx WHERE patient_id=? ORDER BY id DESC", (pid,)),
        atenciones=q("SELECT * FROM atenciones WHERE patient_id=? ORDER BY id DESC", (pid,)),
        notas=q("SELECT * FROM notes WHERE patient_id=? ORDER BY id DESC", (pid,)),
        cotizaciones=q("SELECT * FROM quotes WHERE patient_id=? ORDER BY id DESC", (pid,)),
        abonos=q("SELECT * FROM abonos WHERE patient_id=? ORDER BY id DESC", (pid,)),
        total_abonos=sum(a["monto"] or 0 for a in q("SELECT * FROM abonos WHERE patient_id=?", (pid,))))

@app.post("/pacientes/<int:pid>/plan")
@need("odontologo")
def plan_add(pid):
    f = request.form
    ex("INSERT INTO plan(patient_id,diente,tratamiento,etapa,costo,fecha_prog) VALUES(?,?,?,?,?,?)",
       (pid, f["diente"], f["tratamiento"], f.get("etapa") or "Sin etapa", float(f["costo"] or 0), f.get("fecha_prog") or None))
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

@app.post("/pacientes/<int:pid>/rx")
@need("odontologo", "radiologo")
def rx_add(pid):
    f = request.files["archivo"]
    name = f"{pid}_{dt.datetime.now():%Y%m%d%H%M%S}_{secure_filename(f.filename)}"
    f.save(os.path.join(UPD, name))
    ex("INSERT INTO rx(patient_id,archivo,descripcion,subido_por,fecha) VALUES(?,?,?,?,?)",
       (pid, name, request.form["descripcion"], session["u"], dt.date.today().isoformat()))
    audit("subir_rx", f"paciente {pid}: {name}")
    return redirect(f"/pacientes/{pid}")

@app.route("/logo")
def logo():
    row = q("SELECT value FROM settings WHERE key='logo_file'", one=True)
    if not row or not row["value"]: abort(404)
    return send_from_directory(UPD, row["value"])

@app.route("/rx/<path:n>")
@need("odontologo", "radiologo", "recepcion")
def rx_get(n):
    return send_from_directory(UPD, n, as_attachment=True)

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
    ex("INSERT INTO abonos(patient_id,fecha,monto,concepto,usuario) VALUES(?,?,?,?,?)",
       (pid, dt.date.today().isoformat(), float(f["monto"] or 0), f["concepto"].strip(), session["u"]))
    audit("abono", f"paciente {pid}: {f['concepto']}")
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
    return page("Citas", """<h1>Citas</h1><table><tr><th>Fecha<th>Paciente<th>Odontólogo<th>Motivo<th>Estado<th></tr>{% for c in cs %}<tr><td>{{c.fecha}}<td>{{c.nombre or c.nombre_libre}} {{c.telefono_libre or ''}}
<td>{{c.dentist_name or '—'}}<td>{{c.motivo}}<td>{{c.estado}} {{'(portal)' if c.origen=='portal'}}<td><form method=post action=/citas/{{c.id}} class=row style=margin:0>
<select name=estado>{% for e in ['pendiente','confirmada','atendida','cancelada'] %}<option>{{e}}</option>{% endfor %}</select><button>Cambiar</button></form></tr>{% endfor %}</table>
<h2>Nueva cita</h2><form method=post class=row><select name=patient_id required>{% for p in ps %}<option value={{p.id}}>{{p.nombre}}</option>{% endfor %}</select>
<select name=dentist_id><option value="">Odontólogo (opcional)</option>{% for d in ds %}<option value={{d.id}}>{{d.name}}</option>{% endfor %}</select>
<input type=datetime-local name=fecha required><input name=motivo placeholder=Motivo><button>Agendar cita</button></form>""",
        cs=cs, ps=q("SELECT id,nombre FROM patients ORDER BY nombre"), ds=q("SELECT id,name FROM dentists WHERE active=1 ORDER BY name"))

@app.post("/citas/<int:i>")
@need("odontologo", "recepcion")
def cita_estado(i):
    ex("UPDATE citas SET estado=? WHERE id=?", (request.form["estado"], i)); return redirect("/citas")

@app.route("/recordatorios")
@need("odontologo", "recepcion")
def recordatorios():
    a, b = dt.datetime.now(), dt.datetime.now() + dt.timedelta(days=2)
    cs = q("""SELECT c.*,COALESCE(p.nombre,c.nombre_libre) n,COALESCE(p.telefono,c.telefono_libre) t,p.email FROM citas c LEFT JOIN patients p ON p.id=c.patient_id
              WHERE c.estado IN('pendiente','confirmada') AND c.fecha BETWEEN ? AND ? ORDER BY c.fecha""", (a.strftime("%Y-%m-%d %H:%M"), b.strftime("%Y-%m-%d %H:%M")))
    return page("Recordatorios", """<h1>Citas de las próximas 48 horas</h1><table><tr><th>Fecha<th>Paciente<th>Enviar</tr>{% for c in cs %}<tr><td>{{c.fecha}}<td>{{c.n}}
<td>{% set m='Hola '+c.n+', le recordamos su cita el '+c.fecha+' en Dra. Natalia Morera Odontología. Responda este mensaje para confirmar.' %}
{% if wa(c.t,m) %}<a class=btn href="{{wa(c.t,m)}}" target=_blank>WhatsApp</a>{% endif %}</tr>{% else %}<tr><td colspan=3>No hay citas próximas.</tr>{% endfor %}</table>""", cs=cs)

@app.route("/reactivacion")
@need("odontologo", "recepcion")
def reactivacion():
    lim = (dt.date.today() - dt.timedelta(days=180)).isoformat()
    ps = q("""SELECT * FROM (SELECT p.*,(SELECT MAX(substr(fecha,1,10)) FROM citas c WHERE c.patient_id=p.id AND c.estado='atendida') ultima FROM patients p)
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
    return page("Productos", """<h1>Productos e inventario</h1>
<table><tr><th>Nombre<th>Tipo<th>Precio<th>Costo<th>Stock<th>Mínimo</tr>
{% for p in ps %}<tr{% if p.stock is not none and p.min is not none and p.stock<=p.min %} style="background:#fee"{% endif %}>
<td>{{p.name}}<td>{{p.kind}}<td>${{'{:,.0f}'.format(p.price or 0)}}<td>${{'{:,.0f}'.format(p.cost or 0)}}<td>{{p.stock}}<td>{{p.min}}</tr>
{% else %}<tr><td colspan=6>Sin productos registrados.</tr>{% endfor %}</table>
<h2>Nuevo producto</h2><form method=post class=row>
<input name=name placeholder=Nombre required><select name=kind><option value=insumo>Insumo</option><option value=venta>Producto de venta</option><option value=servicio>Servicio</option></select>
<input name=price type=number step=100 placeholder=Precio><input name=cost type=number step=100 placeholder=Costo>
<input name=stock type=number placeholder=Stock><input name=min type=number placeholder="Stock mínimo"><input name=barcode placeholder="Código de barras">
<button>Guardar producto</button></form>""", ps=ps)

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
PUNTOS_DIENTES = {"18": [43.3, 204.5], "17": [31.0, 172.1], "16": [32.9, 138.9], "15": [49.0, 107.1], "14": [78.2, 78.8], "13": [118.6, 55.7], "12": [167.6, 39.5], "11": [221.9, 31.1], "21": [278.1, 31.1], "22": [332.4, 39.5], "23": [381.4, 55.7], "24": [421.8, 78.8], "25": [451.0, 107.1], "26": [467.1, 138.9], "27": [469.0, 172.1], "28": [456.7, 204.5], "48": [43.3, 125.5], "47": [31.0, 157.9], "46": [32.9, 191.1], "45": [49.0, 222.9], "44": [78.2, 251.2], "43": [118.6, 274.3], "42": [167.6, 290.5], "41": [221.9, 298.9], "31": [278.1, 298.9], "32": [332.4, 290.5], "33": [381.4, 274.3], "34": [421.8, 251.2], "35": [451.0, 222.9], "36": [467.1, 191.1], "37": [469.0, 157.9], "38": [456.7, 125.5]}
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
    if request.method == "POST" and session["rol"] in ("odontologo", "admin"):
        f = request.form
        ex("""INSERT INTO plan(patient_id,diente,tratamiento,etapa,costo,estado,notas,condicion,profesional,fecha_registro)
              VALUES(?,?,?,?,?,?,?,?,?,?)""",
           (pid, f["diente"], f["tratamiento"].strip(), "Odontograma", float(f.get("costo") or 0), "pendiente",
            f.get("notas", ""), f.get("condicion", "tratamiento"), session.get("u"), dt.date.today().isoformat()))
        audit("odontograma_marca", f"paciente {pid} diente {f['diente']} ({f.get('condicion','tratamiento')})")
        return redirect(f"/pacientes/{pid}/odontograma")
    marcas = q("SELECT * FROM plan WHERE patient_id=? ORDER BY id DESC", (pid,))
    ultima_por_diente = {}
    for m in marcas:
        if str(m["diente"]) not in ultima_por_diente:
            ultima_por_diente[str(m["diente"])] = m
    dientes_svg = ""
    for d in DIENTES_ADULTO:
        x, y = PUNTOS_DIENTES[str(d)]
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
        '<svg viewBox="0 0 500 330" style="width:100%;max-width:580px;height:auto;background:linear-gradient(180deg,#fdf2f8,#f8fafc);'
        'border:1px solid var(--line);border-radius:16px;padding:10px;box-shadow:0 1px 4px rgba(15,23,42,.08)">'
        '<ellipse cx="250" cy="165" rx="248" ry="158" fill="none" stroke="#f3a9c7" stroke-width="2" stroke-dasharray="3 5"/>'
        '<line x1="250" y1="25" x2="250" y2="305" stroke="#e2e8f0" stroke-width="1" stroke-dasharray="2 4"/>'
        + dientes_svg + '</svg>'
    )
    leyenda = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:5px;margin:3px 10px 3px 0;font-size:12.5px;color:var(--mute)">'
        f'<span style="width:12px;height:12px;background:{color};border-radius:3px;display:inline-block"></span>{label}</span>'
        for color, label in CONDICIONES.values()
    )
    opciones_select = "".join(f'<option value="{k}">{v[1]}</option>' for k, v in CONDICIONES.items())
    return page(f"Odontograma - {p['nombre']}", """<h1>Odontograma de {{p.nombre}}</h1>
<p><a href="/pacientes/{{p.id}}">&larr; Volver a la ficha del paciente</a></p>
<div class=card style="display:flex;flex-direction:column;align-items:center">""" + svg + """
<div style="margin-top:10px;text-align:center">""" + leyenda + """</div>
<p style="font-size:13px;color:var(--mute)">Haz clic sobre un diente para seleccionarlo abajo. Si un diente tiene varios registros, se muestra el más reciente.</p>
</div>
{% if session.rol in ['odontologo','admin'] %}<div class=card><form method=post class=row>
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
            if ext in (".png", ".jpg", ".jpeg", ".svg", ".webp"):
                nombre_logo = "logo" + ext
                logo.save(os.path.join(UPD, nombre_logo))
                ex("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", ("logo_file", nombre_logo))
                flash("Logo actualizado.")
            else:
                flash("Formato de logo no válido. Usa PNG, JPG, SVG o WEBP.")
        audit("configuracion", "actualizo datos del prestador")
        flash("Configuración guardada.")
    vals = {k: (q("SELECT value FROM settings WHERE key=?", (k,), one=True) or {"value": ""})["value"] for k, _ in campos}
    logo_actual = (q("SELECT value FROM settings WHERE key='logo_file'", one=True) or {"value": ""})["value"]
    return page("Configuración", """<h1>Configuración del prestador</h1>
<form method=post enctype=multipart/form-data class=row style=flex-direction:column;max-width:420px>
{% for k,label in campos %}<label>{{label}}<br><input name="{{k}}" value="{{vals[k]}}" style=width:100%></label>{% endfor %}
<label>Logo del consultorio (aparece en el menú lateral)<br>
{% if logo_actual %}<div style="margin:8px 0"><img src="/logo?v={{logo_actual}}" style="max-height:60px;max-width:160px;border-radius:8px;background:#fff;padding:4px"></div>{% endif %}
<input type=file name=logo accept="image/*"></label>
<button>Guardar</button></form>""", campos=campos, vals=vals, logo_actual=logo_actual)

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
    return page("Reporte RIPS", """<h1>Reporte RIPS para el Ministerio de Salud</h1>
<p>Genera el archivo JSON con las atenciones del periodo, codificadas en CUPS y CIE-10, según la Resolución 948 de 2026.</p>
<form method=post class=row><input type=date name=desde value="{{desde}}"><input type=date name=hasta value="{{hasta}}"><button>Generar RIPS del periodo</button></form>
{% if faltantes %}<div class=msg style=color:#b00>No se generó el archivo. Estos pacientes no tienen documento o fecha de nacimiento completos: {{faltantes|join(', ')}}. Edítalos en su ficha antes de generar el RIPS.</div>{% endif %}
{% if archivo %}<div class=msg>Archivo generado: {{archivo}}. <a href="/rips/descargar/{{archivo}}">Descargarlo</a></div>{% endif %}
<h2>Atenciones en el periodo ({{filas|length}})</h2><table><tr><th>Fecha<th>Paciente<th>CUPS<th>CIE-10<th>Valor</tr>
{% for a in filas %}<tr><td>{{a.fecha}}<td>{{a.nombre}}<td>{{a.cod_cups}}<td>{{a.cod_cie10}}<td>${{'{:,.0f}'.format(a.valor or 0)}}</tr>
{% else %}<tr><td colspan=5>No hay atenciones registradas en este periodo.</tr>{% endfor %}</table>
<h2>Reportes generados antes</h2><table><tr><th>Fecha<th>Periodo<th>Usuarios<th>Registros<th></tr>
{% for l in lotes %}<tr><td>{{l.fecha}}<td>{{l.desde}} a {{l.hasta}}<td>{{l.n_usuarios}}<td>{{l.n_registros}}<td><a href="/rips/descargar/{{l.archivo}}">Descargar</a></tr>{% endfor %}</table>""",
        desde=desde, hasta=hasta, filas=filas, archivo=archivo_generado, lotes=lotes, faltantes=faltantes)

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
    return page("Usuarios", """<h1>Usuarios</h1><table><tr><th>Usuario<th>Nombre<th>Rol</tr>{% for u in us %}<tr><td>{{u.usuario}}<td>{{u.nombre}}<td>{{u.rol}}</tr>{% endfor %}</table>
<h2>Nuevo usuario</h2><form method=post class=row><input name=usuario placeholder=Usuario required><input name=nombre placeholder=Nombre>
<input name=clave type=password placeholder="Contraseña (mínimo 8, letras y números)" required><select name=rol>{% for r in roles %}<option>{{r}}</option>{% endfor %}</select>
<button>Crear usuario</button></form><h2>Auditoría reciente</h2><table><tr><th>Fecha<th>Usuario<th>Acción<th>Detalle</tr>
{% for a in aud %}<tr><td>{{a.fecha}}<td>{{a.usuario}}<td>{{a.accion}}<td>{{a.detalle}}</tr>{% endfor %}</table>""",
        us=q("SELECT * FROM users"), roles=ROLES, aud=q("SELECT * FROM auditoria ORDER BY id DESC LIMIT 50"))

init()
if not USE_PG:
    backup_db()
else:
    print("Consultorio web: usando base de datos PostgreSQL externa (Supabase).")
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

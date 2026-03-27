"""
CoachApp v5 - Dual storage: JSON local / PostgreSQL en Railway
  - Sin DATABASE_URL  → guarda en archivos JSON (desarrollo local)
  - Con DATABASE_URL  → usa PostgreSQL (Railway / producción)

Local:   pip install flask requests
Railway: pip install flask requests psycopg2-binary gunicorn

Env vars opcionales:
  DATABASE_URL    → Railway lo setea automático al agregar PostgreSQL
  ADMIN_EMAIL     → admin@coachapp.com
  ADMIN_PASSWORD  → admin123
  GEMINI_API_KEY  → IA opcional
"""

import json, os, time, requests
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory

app        = Flask(__name__, static_folder=".")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyBmqxLvlKvy4hu7cZaIqPpsxUCoSJrILCM")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
ADMIN_EMAIL    = os.environ.get("ADMIN_EMAIL",    "admin@coachapp.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Domi.kero")
DATABASE_URL   = os.environ.get("DATABASE_URL",   "")
DATA_DIR       = Path("data")

USE_PG = bool(DATABASE_URL)

# ── PostgreSQL (solo si DATABASE_URL está definida) ───────────────────────────
if USE_PG:
    import psycopg2, psycopg2.extras
    def get_db():
        return psycopg2.connect(DATABASE_URL,
                                cursor_factory=psycopg2.extras.RealDictCursor)

# ── Storage API — misma interfaz para JSON y PG ───────────────────────────────
def load(name):
    if USE_PG:
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT data FROM {name}")
            return [r["data"] for r in cur.fetchall()]
        finally:
            conn.close()
    else:
        p = DATA_DIR / f"{name}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []

def save(name, data_list):
    if USE_PG:
        conn = get_db()
        try:
            cur = conn.cursor()
            for item in data_list:
                extra = {}
                if "coach_id"   in item: extra["coach_id"]   = item["coach_id"]
                if "athlete_id" in item: extra["athlete_id"] = item["athlete_id"]
                if "date"       in item: extra["date_col"]   = item["date"]
                cols = ["id", "data"] + list(extra.keys())
                vals = [item["id"], json.dumps(item, ensure_ascii=False)] + list(extra.values())
                ph   = ",".join(["%s"] * len(cols))
                upd  = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c != "id")
                cur.execute(
                    f"INSERT INTO {name} ({','.join(cols)}) VALUES ({ph}) "
                    f"ON CONFLICT (id) DO NOTHING", vals)
            conn.commit()
        finally:
            conn.close()
    else:
        DATA_DIR.mkdir(exist_ok=True)
        (DATA_DIR / f"{name}.json").write_text(
            json.dumps(data_list, ensure_ascii=False, indent=2), encoding="utf-8")

def db_get_one(table, id_val):
    if USE_PG:
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT data FROM {table} WHERE id=%s", (id_val,))
            row = cur.fetchone()
            return row["data"] if row else None
        finally:
            conn.close()
    else:
        return next((x for x in load(table) if x.get("id") == id_val), None)

def db_upsert(table, id_val, data, extra_cols=None):
    if USE_PG:
        conn = get_db()
        try:
            cur = conn.cursor()
            cols = ["id", "data"] + list(extra_cols.keys() if extra_cols else [])
            vals = [id_val, json.dumps(data, ensure_ascii=False)] + list(extra_cols.values() if extra_cols else [])
            ph   = ",".join(["%s"] * len(cols))
            upd  = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c != "id")
            cur.execute(
                f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph}) "
                f"ON CONFLICT (id) DO UPDATE SET {upd}", vals)
            conn.commit()
        finally:
            conn.close()
    else:
        items = load(table)
        idx = next((i for i, x in enumerate(items) if x.get("id") == id_val), None)
        if idx is not None:
            items[idx] = data
        else:
            items.insert(0, data)
        save(table, items)

def db_delete(table, id_val):
    if USE_PG:
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(f"DELETE FROM {table} WHERE id=%s", (id_val,))
            conn.commit()
        finally:
            conn.close()
    else:
        save(table, [x for x in load(table) if x.get("id") != id_val])

def db_query(sql, params=None):
    """PG only — en JSON hace un load+filter manual según el SQL."""
    if USE_PG:
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(sql, params or [])
            return [r["data"] for r in cur.fetchall()]
        finally:
            conn.close()
    else:
        # Parse the table from simple SELECT ... FROM <table> WHERE <col>=%s queries
        import re
        m = re.search(r"FROM\s+(\w+)", sql, re.IGNORECASE)
        if not m:
            return []
        table = m.group(1)
        items = load(table)
        # Handle JOIN queries (sessions + athletes)
        if "JOIN" in sql.upper():
            athletes = load("athletes")
            ath_map = {a["id"]: a for a in athletes}
            if params and len(params) == 1:
                # coach_id filter on athletes join
                coach_id = params[0]
                aid_set = {a["id"] for a in athletes if a.get("coach_id") == coach_id}
                return [s for s in items if s.get("athlete_id") in aid_set]
            if params and len(params) > 0 and "ANY" in sql:
                aid_list = params[0] if isinstance(params[0], list) else [params[0]]
                return [s for s in items if s.get("athlete_id") in aid_list]
        # Simple WHERE col=%s
        where_m = re.findall(r"(\w+)\s*=\s*%s", sql, re.IGNORECASE)
        if not where_m or not params:
            return items
        # Map SQL column names to JSON field names
        col_map = {"coach_id": "coach_id", "athlete_id": "athlete_id",
                   "date_col": "date", "id": "id"}
        result = items
        for col, val in zip(where_m, params if isinstance(params, list) else [params]):
            json_key = col_map.get(col, col)
            if json_key == "routine_id":
                result = [x for x in result if x.get("routine_id") == val]
            else:
                result = [x for x in result if x.get(json_key) == val]
        return result

def uid(prefix): return f"{prefix}-{int(time.time()*1000)}"

# db_get es alias de load — funciona en ambos modos
def db_get(table, filters=None):
    if USE_PG:
        conn = get_db()
        try:
            cur = conn.cursor()
            if filters:
                where = " AND ".join(f"{k}=%s" for k in filters)
                cur.execute(f"SELECT data FROM {table} WHERE {where}", list(filters.values()))
            else:
                cur.execute(f"SELECT data FROM {table}")
            return [r["data"] for r in cur.fetchall()]
        finally:
            conn.close()
    else:
        items = load(table)
        if filters:
            for k, v in filters.items():
                items = [x for x in items if x.get(k) == v]
        return items

# ── DB INIT ───────────────────────────────────────────────────────────────────
def init_db():
    if not USE_PG:
        DATA_DIR.mkdir(exist_ok=True)
        print("  ✓ Modo JSON local (sin DATABASE_URL)")
        return
    conn = get_db()
    try:
        cur = conn.cursor()
        tables = {
            "coaches":   "id TEXT PRIMARY KEY, data JSONB NOT NULL",
            "athletes":  "id TEXT PRIMARY KEY, data JSONB NOT NULL, coach_id TEXT",
            "exercises": "id TEXT PRIMARY KEY, data JSONB NOT NULL",
            "routines":  "id TEXT PRIMARY KEY, data JSONB NOT NULL, coach_id TEXT",
            "sessions":  "id TEXT PRIMARY KEY, data JSONB NOT NULL, athlete_id TEXT",
            "schedules": "id TEXT PRIMARY KEY, data JSONB NOT NULL, athlete_id TEXT, coach_id TEXT, date_col TEXT",
            "posts":     "id TEXT PRIMARY KEY, data JSONB NOT NULL, coach_id TEXT",
        }
        for tbl, cols in tables.items():
            cur.execute(f"CREATE TABLE IF NOT EXISTS {tbl} ({cols})")
        conn.commit()
        print("  ✓ Tablas PostgreSQL listas")
    finally:
        conn.close()

# ── CORS ──────────────────────────────────────────────────────────────────────
@app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"]  = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type"
    r.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    return r

@app.route("/<path:p>", methods=["OPTIONS"])
def opts(p): return jsonify({}), 200

@app.route("/api/coach/self-schedule", methods=["GET"])
def get_coach_self_schedule():
    coach_id = request.args.get("coach_id","")
    if not coach_id: return jsonify([])
    athlete_id = "coach-self-"+coach_id
    all_s = load("schedules")
    items = [s for s in all_s if s.get("athlete_id")==athlete_id]
    routines_map = {r["id"]:r for r in load("routines")}
    for s in items: s["routine"] = routines_map.get(s.get("routine_id",""))
    return jsonify(items)

@app.route("/api/coach/self-schedule", methods=["POST"])
def create_coach_self_schedule():
    d = request.json or {}
    coach_id = d.get("coach_id","")
    athlete_id = "coach-self-"+coach_id
    dates = d.get("dates",[])
    routine_id = d.get("routine_id","")
    created = []
    for date in dates:
        all_s = load("schedules")
        dup = next((s for s in all_s if s.get("athlete_id")==athlete_id
                    and s.get("date")==date and s.get("routine_id")==routine_id), None)
        if not dup:
            new_s = {"id":uid("sch"),"athlete_id":athlete_id,"routine_id":routine_id,
                     "coach_id":coach_id,"date":date,"completed":False,"seen":True,
                     "created_at":datetime.now().isoformat()}
            db_upsert("schedules", new_s["id"], new_s,
                      {"athlete_id":athlete_id,"coach_id":coach_id,"date_col":date})
            created.append(new_s)
    return jsonify({"ok":True,"created":len(created)})

@app.route("/api/coach/self-schedule/<sid>/complete", methods=["PUT"])
def complete_coach_self_schedule(sid):
    s = db_get_one("schedules", sid)
    if not s: return jsonify({"error":"not found"}),404
    s["completed"] = True
    db_upsert("schedules", sid, s,
              {"athlete_id":s["athlete_id"],"coach_id":s.get("coach_id",""),"date_col":s["date"]})
    return jsonify({"ok":True})

@app.route("/api/admin/reseed", methods=["POST"])
def reseed():
    """Fuerza recarga del seed. Solo usar desde Railway para inicializar datos."""
    d = request.json or {}
    if d.get("secret") != ADMIN_PASSWORD:
        return jsonify({"error": "unauthorized"}), 401
    created = []
    # Force reseed exercises and routines (never overwrite coaches/athletes/sessions)
    if USE_PG:
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) as n FROM exercises")
            ex_count = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) as n FROM routines")
            rut_count = cur.fetchone()["n"]
        finally:
            conn.close()
    else:
        ex_count = len(load("exercises"))
        rut_count = len(load("routines"))

    if ex_count < 50:
        save("exercises", EXERCISES_SEED)
        created.append(f"{len(EXERCISES_SEED)} ejercicios")
    if rut_count < 10:
        save("routines", ROUTINES_SEED)
        created.append(f"{len(ROUTINES_SEED)} rutinas")
    if not load("coaches"):
        save("coaches", COACHES_SEED)
        created.append(f"{len(COACHES_SEED)} coaches seed")

    return jsonify({"ok": True, "created": created, "exercises": ex_count, "routines": rut_count})

@app.route("/api/ai/status")
def ai_status():
    return jsonify({"available": bool(GEMINI_KEY)})

@app.route("/api/ai/weather-rec", methods=["POST"])
def weather_rec():
    if not GEMINI_KEY: return jsonify({"recommendation":""})
    d = request.json or {}
    w = d.get("weather",{})
    prompt = (
        "Sos un entrenador deportivo. El atleta va a entrenar con la rutina: "
        + '"' + d.get("routine_name","") + '"'
        + " (tipo: " + d.get("routine_type","")
        + ", " + str(d.get("exercise_count",0)) + " ejercicios"
        + ", dificultad: " + d.get("difficulty","") + ").\n"
        + "Condiciones climaticas: " + str(round(w.get("temp",20))) + "C"
        + ", sensacion termica " + str(round(w.get("apparent",20))) + "C"
        + ", humedad " + str(w.get("humidity",50)) + "%"
        + ", " + w.get("desc","Despejado") + ".\n"
        + "Dame UNA recomendacion concreta de maximo 2 oraciones sobre como adaptar el entrenamiento. "
        + "Se especifico y util, no generico."
    )
    try:
        r = requests.post(
            f"{GEMINI_URL}?key={GEMINI_KEY}",
            json={"contents":[{"parts":[{"text":prompt}]}]},
            timeout=10
        )
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        return jsonify({"recommendation": text})
    except Exception as e:
        return jsonify({"recommendation": "", "error": str(e)})

@app.route("/favicon.ico")
def favicon(): return "", 204

@app.route("/manifest.json")
def manifest(): return send_from_directory(".", "manifest.json")

@app.route("/sw.js")
def service_worker():
    resp = send_from_directory(".", "sw.js")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp

@app.route("/icon-<size>.png")
def icon(size):
    # Serve icon if exists, otherwise return empty 1x1 PNG
    import os
    path = f"icon-{size}.png"
    if os.path.exists(path):
        return send_from_directory(".", path)
    # Minimal valid PNG (1x1 black pixel)
    import base64
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    from flask import Response
    return Response(png, mimetype="image/png")

@app.route("/")
def index(): return send_from_directory(".", "index.html")

# ═══════════════════════════════════════════════════════════════════════════════
# SEED DATA
# ═══════════════════════════════════════════════════════════════════════════════
COACHES_SEED = [
    {"id":"coach-001","name":"Carlos Martínez","email":"coach@coachapp.com",
     "password":"coach123","specialty":"Fitness y Padel","avatar":"https://i.pravatar.cc/150?u=coach1",
     "is_disabled":False,"status":"active","created_at":"2026-01-01T10:00:00"},
    {"id":"coach-002","name":"Laura Díaz","email":"laura@coachapp.com",
     "password":"laura123","specialty":"Running y Atletismo","avatar":"https://i.pravatar.cc/150?u=coach2",
     "is_disabled":False,"status":"active","created_at":"2026-01-02T10:00:00"},
]

ATHLETES_SEED = [
  {"id":"ath-001","first_name":"Juan","last_name":"Pérez","email":"juan@email.com",
   "phone":"+54 9 11 1111-1111","sport":"Padel","level":"intermediate","age":28,
   "height":178,"weight":78,"goal":"Mejorar resistencia y técnica en padel competitivo",
   "notes":"Entrena martes, jueves y sábados.","hand":"derecho","padel_pos":"drive",
   "password":"juan123","avatar":"https://i.pravatar.cc/150?u=juan",
   "status":"active","is_disabled":False,"training_id":"","coach_id":"coach-001",
   "created_at":"2026-01-10T10:00:00"},
  {"id":"ath-002","first_name":"María","last_name":"González","email":"maria@email.com",
   "phone":"+54 9 11 2222-2222","sport":"Fitness General","level":"beginner","age":32,
   "height":165,"weight":68,"goal":"Bajar de peso y tonificar",
   "notes":"Lunes, miércoles y viernes.","hand":"derecho","padel_pos":"drive",
   "password":"maria123","avatar":"https://i.pravatar.cc/150?u=maria",
   "status":"active","is_disabled":False,"training_id":"","coach_id":"coach-001",
   "created_at":"2026-01-12T10:00:00"},
  {"id":"ath-003","first_name":"Carlos","last_name":"López","email":"carlos@email.com",
   "phone":"+54 9 11 3333-3333","sport":"Running","level":"advanced","age":35,
   "height":182,"weight":75,"goal":"Sub 40 minutos en 10K",
   "notes":"50km semanales. Agregar fuerza.","hand":"derecho","padel_pos":"drive",
   "password":"carlos123","avatar":"https://i.pravatar.cc/150?u=carlos",
   "status":"active","is_disabled":False,"training_id":"","coach_id":"coach-002",
   "created_at":"2026-01-14T10:00:00"},
]

EXERCISES_SEED = [
  {"id":"ex-001","name":"Sentadilla","category":"fuerza","muscle_group":"cuadriceps","muscle_groups":["cuadriceps","gluteos","isquiotibiales","core"],"equipment":"Barra, Peso Corporal","difficulty":"intermediate","description":"Ejercicio rey del tren inferior.","tips":["Mantené el torso erguido","Rodillas alineadas con los pies","Bajá hasta paralelo"],"errors":["Valgo de rodillas","Talones levantados","Espalda redondeada"],"tags":["Bilateral","Fuerza","Piernas"],"image":"https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=600","created_by":"system"},
  {"id":"ex-002","name":"Peso Muerto","category":"fuerza","muscle_group":"gluteos","muscle_groups":["gluteos","isquiotibiales","espalda","core"],"equipment":"Barra, Mancuernas","difficulty":"intermediate","description":"Bisagra de cadera para cadena posterior completa.","tips":["Iniciá con la cadera","Barra cerca del cuerpo","Core apretado"],"errors":["Redondear la lumbar","Hiperextender en la cima","Arrancar con la espalda"],"tags":["Cadena Posterior","Fuerza"],"image":"https://images.unsplash.com/photo-1597452485669-2c7bb5fef90d?w=600","created_by":"system"},
  {"id":"ex-003","name":"Sentadilla Búlgara","category":"fuerza","muscle_group":"cuadriceps","muscle_groups":["cuadriceps","gluteos"],"equipment":"Mancuernas","difficulty":"intermediate","description":"Sentadilla unilateral. Máxima activación glútea.","tips":["Pie delantero bien adelante","Bajá verticalmente","Cadera neutra"],"errors":["Tronco muy inclinado","Rodilla en valgo","Pie trasero cerca"],"tags":["Unilateral","Glúteos"],"image":"https://images.unsplash.com/photo-1571019614242-c5c5dee9f50b?w=600","created_by":"system"},
  {"id":"ex-004","name":"Hip Thrust","category":"fuerza","muscle_group":"gluteos","muscle_groups":["gluteos","isquiotibiales","core"],"equipment":"Barra","difficulty":"beginner","description":"Aislamiento máximo de glúteos.","tips":["Barbilla al pecho arriba","Apretá los glúteos","Pies a ancho de caderas"],"errors":["Hiperextender la lumbar","No llegar a extensión","Talones levantados"],"tags":["Glúteos","Fuerza"],"image":"https://images.unsplash.com/photo-1581009146145-b5ef050c2e1e?w=600","created_by":"system"},
  {"id":"ex-005","name":"Zancada","category":"fuerza","muscle_group":"cuadriceps","muscle_groups":["cuadriceps","gluteos"],"equipment":"Peso Corporal, Mancuernas","difficulty":"beginner","description":"Paso largo con flexión bilateral.","tips":["Torso erguido","Rodilla trasera cerca del piso","Paso largo"],"errors":["Rodilla pasa el pie","Tronco inclinado","Pasos cortos"],"tags":["Unilateral","Funcional"],"image":"https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600","created_by":"system"},
  {"id":"ex-006","name":"Press de Banca","category":"hipertrofia","muscle_group":"pecho","muscle_groups":["pecho","triceps","hombros"],"equipment":"Barra, Mancuernas","difficulty":"intermediate","description":"Empuje horizontal para pectoral mayor.","tips":["Retrae las escápulas","Codos a 45°","Pies firmes"],"errors":["Arquear la lumbar","Rebotar la barra","Codos a 90°"],"tags":["Pecho","Empuje"],"image":"https://images.unsplash.com/photo-1571019614242-c5c5dee9f50b?w=600","created_by":"system"},
  {"id":"ex-007","name":"Press Inclinado","category":"hipertrofia","muscle_group":"pecho","muscle_groups":["pecho","triceps","hombros"],"equipment":"Mancuernas","difficulty":"intermediate","description":"Empuje inclinado para pectoral superior.","tips":["Banco a 30-45°","Rotar al subir","Bajar con control"],"errors":["Banco muy vertical","No bajar suficiente","Sacudir"],"tags":["Pecho Superior","Mancuernas"],"image":"https://images.unsplash.com/photo-1517838277536-f5f99be501cd?w=600","created_by":"system"},
  {"id":"ex-008","name":"Flexiones","category":"fuerza","muscle_group":"pecho","muscle_groups":["pecho","triceps","core"],"equipment":"Peso Corporal","difficulty":"beginner","description":"Empuje horizontal con peso corporal.","tips":["Cuerpo rígido","Codos a 45°","Bajar hasta el suelo"],"errors":["Cadera caída","Codos a 90°","Rango incompleto"],"tags":["Pecho","Peso Corporal"],"image":"https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600","created_by":"system"},
  {"id":"ex-009","name":"Dominadas","category":"fuerza","muscle_group":"espalda","muscle_groups":["espalda","biceps"],"equipment":"Barra fija","difficulty":"advanced","description":"Tracción vertical. Rey del dorsal ancho.","tips":["Deprimir escápulas primero","Pecho al frente","Controlá la bajada"],"errors":["Kipping","Solo brazos","Encogerse abajo"],"tags":["Espalda","Tracción"],"image":"https://images.unsplash.com/photo-1526506118085-60ce8714f8c5?w=600","created_by":"system"},
  {"id":"ex-010","name":"Remo con Barra","category":"fuerza","muscle_group":"espalda","muscle_groups":["espalda","biceps"],"equipment":"Barra","difficulty":"intermediate","description":"Tracción horizontal para dorsal y romboides.","tips":["Espalda paralela","Codos pegados","Escápulas en la cima"],"errors":["Redondear la espalda","Usar impulso","Codos abiertos"],"tags":["Espalda","Tracción"],"image":"https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=600","created_by":"system"},
  {"id":"ex-011","name":"Jalón al Pecho","category":"hipertrofia","muscle_group":"espalda","muscle_groups":["espalda","biceps"],"equipment":"Poleas","difficulty":"beginner","description":"Tracción vertical en polea.","tips":["Pecho adelante","Iniciá con codos","Controlá la subida"],"errors":["Jalar detrás nuca","Usar inercia","Encorvar"],"tags":["Espalda","Máquina"],"image":"https://images.unsplash.com/photo-1581009146145-b5ef050c2e1e?w=600","created_by":"system"},
  {"id":"ex-012","name":"Press Militar","category":"hipertrofia","muscle_group":"hombros","muscle_groups":["hombros","triceps"],"equipment":"Barra, Mancuernas","difficulty":"intermediate","description":"Empuje vertical para deltoides.","tips":["Core apretado","Cabeza adelante al subir","Codos adelante"],"errors":["Arquear la espalda","Codos muy atrás","Sin extensión completa"],"tags":["Hombros","Empuje"],"image":"https://images.unsplash.com/photo-1517838277536-f5f99be501cd?w=600","created_by":"system"},
  {"id":"ex-013","name":"Elevaciones Laterales","category":"hipertrofia","muscle_group":"hombros","muscle_groups":["hombros"],"equipment":"Mancuernas","difficulty":"beginner","description":"Aislamiento del deltoides lateral.","tips":["Codos semi-flexionados","Meñique arriba","Controlá la bajada"],"errors":["Subir más de horizontal","Balancear","Encogerse"],"tags":["Hombros","Aislamiento"],"image":"https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=600","created_by":"system"},
  {"id":"ex-014","name":"Curl de Bíceps","category":"hipertrofia","muscle_group":"biceps","muscle_groups":["biceps"],"equipment":"Mancuernas, Barra","difficulty":"beginner","description":"Aislamiento clásico del bíceps.","tips":["Codos fijos","Supina al contraer","Controlá la bajada"],"errors":["Balancear el torso","Bajar sin control","Codos adelante"],"tags":["Bíceps","Aislamiento"],"image":"https://images.unsplash.com/photo-1581009146145-b5ef050c2e1e?w=600","created_by":"system"},
  {"id":"ex-015","name":"Extensión Tríceps Polea","category":"hipertrofia","muscle_group":"triceps","muscle_groups":["triceps"],"equipment":"Poleas","difficulty":"beginner","description":"Aislamiento de las tres cabezas del tríceps.","tips":["Codos fijos","Extensión completa","Controlá la subida"],"errors":["Mover codos","Balancear","Sin extensión"],"tags":["Tríceps","Aislamiento"],"image":"https://images.unsplash.com/photo-1517838277536-f5f99be501cd?w=600","created_by":"system"},
  {"id":"ex-016","name":"Fondos en Paralelas","category":"fuerza","muscle_group":"triceps","muscle_groups":["triceps","pecho"],"equipment":"Paralelas","difficulty":"advanced","description":"Empuje corporal para tríceps y pecho.","tips":["Torso inclinado","Codos atrás","Rango completo"],"errors":["Hombros encogidos","Balancearse","Sin control"],"tags":["Tríceps","Peso Corporal"],"image":"https://images.unsplash.com/photo-1526506118085-60ce8714f8c5?w=600","created_by":"system"},
  {"id":"ex-017","name":"Plancha","category":"core","muscle_group":"core","muscle_groups":["core","hombros"],"equipment":"Peso Corporal","difficulty":"beginner","description":"Isométrico fundamental para el core.","tips":["Core activado","Columna neutral","Respirá con control"],"errors":["Caer la cadera","Glúteos arriba","Retener la respiración"],"tags":["Core","Isométrico"],"image":"https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600","created_by":"system"},
  {"id":"ex-018","name":"Dead Bug","category":"core","muscle_group":"core","muscle_groups":["core"],"equipment":"Peso Corporal","difficulty":"beginner","description":"Contralateral para activar el transverso.","tips":["Lumbar en el suelo","Movimiento lento","Exhalá al extender"],"errors":["Despegar la lumbar","Ir rápido","Sin coordinación"],"tags":["Core","Antirotación"],"image":"https://images.unsplash.com/photo-1571019614242-c5c5dee9f50b?w=600","created_by":"system"},
  {"id":"ex-019","name":"Russian Twist","category":"core","muscle_group":"core","muscle_groups":["core","oblicuos"],"equipment":"Peso Corporal","difficulty":"beginner","description":"Rotación de tronco para oblicuos.","tips":["Pies elevados","Rotar desde el tronco","Espalda recta"],"errors":["Solo mover brazos","Espalda redondeada","Tronco muy bajo"],"tags":["Core","Oblicuos"],"image":"https://images.unsplash.com/photo-1517838277536-f5f99be501cd?w=600","created_by":"system"},
  {"id":"ex-020","name":"Rueda Abdominal","category":"core","muscle_group":"core","muscle_groups":["core","hombros"],"equipment":"Rueda abdominal","difficulty":"advanced","description":"Anti-extensión lumbar de alta demanda.","tips":["Desde rodillas","Columna neutral","Core máximo"],"errors":["Caer la cadera","Ir demasiado lejos","Solo con brazos"],"tags":["Core","Anti-extensión"],"image":"https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=600","created_by":"system"},
  {"id":"ex-021","name":"Burpees","category":"cardio","muscle_group":"cuerpo_completo","muscle_groups":["cuadriceps","pecho","core"],"equipment":"Peso Corporal","difficulty":"intermediate","description":"Ejercicio metabólico completo.","tips":["Rodillas semi-flex al aterrizar","Espalda plana en plancha","Explota al saltar"],"errors":["Piernas rectas al aterrizar","Cadera caída","Técnica deficiente"],"tags":["Cardio","Explosivo"],"image":"https://images.unsplash.com/photo-1571019614242-c5c5dee9f50b?w=600","created_by":"system"},
  {"id":"ex-022","name":"Box Jump","category":"potencia","muscle_group":"cuadriceps","muscle_groups":["cuadriceps","gluteos","gemelos"],"equipment":"Cajón pliométrico","difficulty":"intermediate","description":"Salto explosivo al cajón.","tips":["Brazos hacia atrás","Aterrizar suave","Bajar caminando"],"errors":["Rodillas en valgo","Sin brazos","Bajar saltando"],"tags":["Potencia","Explosivo"],"image":"https://images.unsplash.com/photo-1526506118085-60ce8714f8c5?w=600","created_by":"system"},
  {"id":"ex-023","name":"Salto a la Cuerda","category":"cardio","muscle_group":"gemelos","muscle_groups":["gemelos","core"],"equipment":"Cuerda de salto","difficulty":"beginner","description":"Cardio coordinativo económico y versátil.","tips":["Codos cerca","Saltá mínimo","Metatarso al aterrizar"],"errors":["Saltar alto","Brazos completos","Mirar la cuerda"],"tags":["Cardio","Coordinación"],"image":"https://images.unsplash.com/photo-1581009146145-b5ef050c2e1e?w=600","created_by":"system"},
  {"id":"ex-024","name":"Hip 90/90","category":"movilidad","muscle_group":"gluteos","muscle_groups":["gluteos","rotadores de cadera"],"equipment":"Peso Corporal","difficulty":"beginner","description":"Movilidad de cadera en rotación interna y externa.","tips":["Ambas piernas a 90°","60 segundos por lado","Respirá profundo"],"errors":["Levantar la cadera","Forzar sin respirar","Hacerlo rápido"],"tags":["Movilidad","Cadera"],"image":"https://images.unsplash.com/photo-1517838277536-f5f99be501cd?w=600","created_by":"system"},
  {"id":"ex-025","name":"World's Greatest Stretch","category":"movilidad","muscle_group":"cuerpo_completo","muscle_groups":["cadera","dorsales","isquiotibiales"],"equipment":"Peso Corporal","difficulty":"beginner","description":"El mejor calentamiento dinámico integral.","tips":["Con fluidez","3-5 respiraciones","Progresá el rango"],"errors":["Solo estático","Demasiado rápido","Forzar"],"tags":["Movilidad","Calentamiento"],"image":"https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=600","created_by":"system"},
  {"id":"ex-026","name":"Rotación con Palo","category":"padel_specific","muscle_group":"core","muscle_groups":["core","oblicuos","hombros"],"equipment":"Palo, Pica","difficulty":"beginner","description":"Rotación de tronco para la mecánica del golpe en padel.","tips":["Iniciá desde caderas","Pies firmes","Acompañá con el hombro trasero"],"errors":["Solo el torso","Levantar el talón","Mover la cabeza"],"tags":["Padel","Rotacional"],"image":"https://images.unsplash.com/photo-1526506118085-60ce8714f8c5?w=600","created_by":"system"},
  {"id":"ex-027","name":"Lateral Bound","category":"potencia","muscle_group":"gluteos","muscle_groups":["gluteos","abductores","gemelos"],"equipment":"Peso Corporal","difficulty":"intermediate","description":"Salto lateral explosivo. Simula desplazamientos en padel.","tips":["Aterrizá en un pie","Empujá desde el glúteo","Tronco estable"],"errors":["Rodilla en valgo","Tronco inclinado","Pasos cortos"],"tags":["Padel","Lateral","Explosivo"],"image":"https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600","created_by":"system"},
  {"id":"ex-028","name":"Split Step","category":"padel_specific","muscle_group":"gemelos","muscle_groups":["gemelos","cuadriceps","core"],"equipment":"Peso Corporal","difficulty":"beginner","description":"Salto de preparación sincronizado con el golpe rival.","tips":["Saltá cuando el rival toca","Aterrizá a ancho de hombros","Espera activa"],"errors":["No sincronizar","Pies juntos","Sin split step"],"tags":["Padel","Técnico","Reactividad"],"image":"https://images.unsplash.com/photo-1581009146145-b5ef050c2e1e?w=600","created_by":"system"},
  {"id": "ex-029", "name": "Sentadilla Goblet", "category": "fuerza", "muscle_group": "cuadriceps", "muscle_groups": ["cuadriceps", "gluteos", "core"], "equipment": "Pesa Rusa", "difficulty": "beginner", "description": "Sentadilla frontal con pesa rusa. Ideal para aprender mecánica.", "tips": ["Codo debajo de la pesa", "Pecho erguido", "Talones en el piso"], "errors": ["Inclinarse hacia adelante", "Rodillas adentro", "Lumbar redondeada"], "tags": ["Fuerza", "Tren Inferior", "Principiante"], "image": "https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=600", "created_by": "system"},
  {"id": "ex-030", "name": "Peso Muerto Rumano", "category": "fuerza", "muscle_group": "isquiotibiales", "muscle_groups": ["isquiotibiales", "gluteos", "espalda"], "equipment": "Barra, Mancuernas", "difficulty": "intermediate", "description": "Bisagra de cadera con énfasis en isquiotibiales.", "tips": ["Barra roza las piernas", "Caderas empujan atrás", "Rodillas suavemente flexionadas"], "errors": ["Redondear espalda", "Doblar rodillas en exceso", "Bajar demasiado"], "tags": ["Cadena Posterior", "Fuerza"], "image": "https://images.unsplash.com/photo-1597452485669-2c7bb5fef90d?w=600", "created_by": "system"},
  {"id": "ex-031", "name": "Step Up con Mancuernas", "category": "fuerza", "muscle_group": "cuadriceps", "muscle_groups": ["cuadriceps", "gluteos", "gemelos"], "equipment": "Mancuernas", "difficulty": "beginner", "description": "Subida al cajón unilateral. Excelente para equilibrio y fuerza funcional.", "tips": ["Apoyo completo del pie", "Empujá con el talón", "Tronco erguido"], "errors": ["Impulso con pie trasero", "Caída del tronco", "Cajón demasiado bajo"], "tags": ["Unilateral", "Funcional"], "image": "https://images.unsplash.com/photo-1526506118085-60ce8714f8c5?w=600", "created_by": "system"},
  {"id": "ex-032", "name": "Prensa de Piernas", "category": "hipertrofia", "muscle_group": "cuadriceps", "muscle_groups": ["cuadriceps", "gluteos", "isquiotibiales"], "equipment": "Máquina", "difficulty": "beginner", "description": "Cuádriceps e isquiotibiales con máquina guiada.", "tips": ["Pies a ancho de caderas", "Rodillas alineadas", "No bloquear articulación"], "errors": ["Despegar la pelvis", "Rango incompleto", "Pies muy altos"], "tags": ["Máquina", "Tren Inferior"], "image": "https://images.unsplash.com/photo-1581009146145-b5ef050c2e1e?w=600", "created_by": "system"},
  {"id": "ex-033", "name": "Curl de Isquiotibiales", "category": "hipertrofia", "muscle_group": "isquiotibiales", "muscle_groups": ["isquiotibiales", "gluteos"], "equipment": "Máquina", "difficulty": "beginner", "description": "Aislamiento de isquiotibiales en máquina.", "tips": ["Cadera pegada al banco", "Tobillo bajo el rodillo", "Movimiento controlado"], "errors": ["Levantar la cadera", "Usar impulso", "Sin rango completo"], "tags": ["Máquina", "Aislamiento"], "image": "https://images.unsplash.com/photo-1581009146145-b5ef050c2e1e?w=600", "created_by": "system"},
  {"id": "ex-034", "name": "Extensión de Cuádriceps", "category": "hipertrofia", "muscle_group": "cuadriceps", "muscle_groups": ["cuadriceps"], "equipment": "Máquina", "difficulty": "beginner", "description": "Aislamiento de cuádriceps. Ideal para prehabilitación de rodilla.", "tips": ["Ajustar el respaldo", "Movimiento lento", "Contracción arriba"], "errors": ["Usar impulso", "Rango incompleto", "Peso excesivo"], "tags": ["Máquina", "Aislamiento", "Rodilla"], "image": "https://images.unsplash.com/photo-1581009146145-b5ef050c2e1e?w=600", "created_by": "system"},
  {"id": "ex-035", "name": "Elevación de Gemelos de Pie", "category": "hipertrofia", "muscle_group": "gemelos", "muscle_groups": ["gemelos"], "equipment": "Máquina, Peso Corporal", "difficulty": "beginner", "description": "Trabajo de gemelos en rango completo.", "tips": ["Rango completo abajo", "Pausa arriba 1 segundo", "Bajar con control"], "errors": ["Rebotar abajo", "Rodillas flexionadas", "Sin rango completo"], "tags": ["Gemelos", "Aislamiento"], "image": "https://images.unsplash.com/photo-1581009146145-b5ef050c2e1e?w=600", "created_by": "system"},
  {"id": "ex-036", "name": "Abducción de Cadera", "category": "prevencion", "muscle_group": "abductores", "muscle_groups": ["abductores", "gluteo_medio"], "equipment": "Máquina, Bandas Elásticas", "difficulty": "beginner", "description": "Glúteo medio y abductores. Clave para estabilidad de rodilla.", "tips": ["Movimiento controlado", "Sin inclinar el tronco", "Activar glúteo medio"], "errors": ["Usar impulso", "Tronco inclinado", "Rango corto"], "tags": ["Prevención", "Rodilla", "Cadera"], "image": "https://images.unsplash.com/photo-1581009146145-b5ef050c2e1e?w=600", "created_by": "system"},
  {"id": "ex-037", "name": "Sentadilla Sumo", "category": "fuerza", "muscle_group": "aductores", "muscle_groups": ["aductores", "cuadriceps", "gluteos"], "equipment": "Barra, Pesa Rusa", "difficulty": "intermediate", "description": "Variante amplia que activa aductores y glúteos.", "tips": ["Pies apuntando 45°", "Rodillas siguen los pies", "Tronco erguido"], "errors": ["Valgo de rodillas", "Talones levantados", "Espalda redondeada"], "tags": ["Fuerza", "Tren Inferior"], "image": "https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=600", "created_by": "system"},
  {"id": "ex-038", "name": "Peso Muerto Sumo", "category": "fuerza", "muscle_group": "aductores", "muscle_groups": ["aductores", "gluteos", "isquiotibiales", "espalda"], "equipment": "Barra", "difficulty": "intermediate", "description": "Variante sumo del peso muerto. Mayor activación de aductores.", "tips": ["Stance amplio", "Grip neutro o doble prono", "Caderas cerca de la barra"], "errors": ["Rodillas cediendo", "Espalda redondeada", "Caderas muy altas"], "tags": ["Fuerza", "Cadena Posterior"], "image": "https://images.unsplash.com/photo-1597452485669-2c7bb5fef90d?w=600", "created_by": "system"},
  {"id": "ex-039", "name": "Zancada Lateral", "category": "fuerza", "muscle_group": "aductores", "muscle_groups": ["aductores", "cuadriceps", "gluteos"], "equipment": "Peso Corporal, Mancuernas", "difficulty": "intermediate", "description": "Desplazamiento lateral en plano frontal. Transferencia alta a deportes de raqueta.", "tips": ["Pie lateral apuntando afuera", "Cadera empuja atrás", "Rodilla sobre el pie"], "errors": ["Tronco inclinado", "Rodilla adentro", "Sin profundidad"], "tags": ["Unilateral", "Lateral", "Padel"], "image": "https://images.unsplash.com/photo-1581009146145-b5ef050c2e1e?w=600", "created_by": "system"},
  {"id": "ex-040", "name": "Nordic Curl", "category": "prevencion", "muscle_group": "isquiotibiales", "muscle_groups": ["isquiotibiales"], "equipment": "Banco, Compañero", "difficulty": "advanced", "description": "Excéntrico de isquiotibiales. Principal ejercicio de prevención de lesiones de isquio.", "tips": ["Caída lenta controlada", "Activar glúteos", "Manos para frenar"], "errors": ["Caída libre", "Cadera no alineada", "Velocidad excesiva"], "tags": ["Prevención", "Isquiotibiales", "Excéntrico"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-041", "name": "Press Banca Agarre Cerrado", "category": "hipertrofia", "muscle_group": "triceps", "muscle_groups": ["triceps", "pecho"], "equipment": "Barra", "difficulty": "intermediate", "description": "Variante de banca para máxima activación de tríceps.", "tips": ["Agarre al ancho de hombros", "Codos pegados al cuerpo", "Extensión completa"], "errors": ["Codos abiertos", "Barra rebotar en pecho", "Muñecas flexionadas"], "tags": ["Tríceps", "Empuje"], "image": "https://images.unsplash.com/photo-1571019614242-c5c5dee9f50b?w=600", "created_by": "system"},
  {"id": "ex-042", "name": "Remo en Polea Baja", "category": "hipertrofia", "muscle_group": "espalda", "muscle_groups": ["espalda", "biceps", "romboides"], "equipment": "Poleas", "difficulty": "beginner", "description": "Tracción horizontal en polea. Ideal para romboides y trapecio medio.", "tips": ["Codos pegados", "Escápulas al final", "Tronco estático"], "errors": ["Balancear el torso", "Codos abiertos", "Sin retracción escapular"], "tags": ["Espalda", "Tracción"], "image": "https://images.unsplash.com/photo-1597452485669-2c7bb5fef90d?w=600", "created_by": "system"},
  {"id": "ex-043", "name": "Face Pull", "category": "prevencion", "muscle_group": "hombros", "muscle_groups": ["hombros", "trapecio", "rotadores"], "equipment": "Poleas", "difficulty": "beginner", "description": "Rotación externa de hombro. Clave para salud del manguito rotador.", "tips": ["Codos a nivel de hombros", "Rotar externamente al final", "Movimiento lento"], "errors": ["Codos bajos", "Tirar con brazos", "Demasiado peso"], "tags": ["Prevención", "Hombro", "Manguito"], "image": "https://images.unsplash.com/photo-1517838277536-f5f99be501cd?w=600", "created_by": "system"},
  {"id": "ex-044", "name": "Rotación Externa con Banda", "category": "prevencion", "muscle_group": "hombros", "muscle_groups": ["rotadores", "hombros"], "equipment": "Bandas Elásticas", "difficulty": "beginner", "description": "Fortalece el manguito rotador. Previene lesiones en hombro y codo.", "tips": ["Codo pegado al cuerpo", "Movimiento lento y controlado", "Sin compensar con tronco"], "errors": ["Mover el codo", "Demasiada velocidad", "Rango corto"], "tags": ["Prevención", "Manguito Rotador"], "image": "https://images.unsplash.com/photo-1517838277536-f5f99be501cd?w=600", "created_by": "system"},
  {"id": "ex-045", "name": "Elevación Frontal", "category": "hipertrofia", "muscle_group": "hombros", "muscle_groups": ["hombros", "trapecio"], "equipment": "Mancuernas", "difficulty": "beginner", "description": "Deltoides anterior. Complementa el trabajo de empuje.", "tips": ["Pulgar arriba", "No superar la horizontal", "Bajar controlado"], "errors": ["Balancear el cuerpo", "Subir demasiado", "Usar impulso"], "tags": ["Hombros", "Aislamiento"], "image": "https://images.unsplash.com/photo-1517838277536-f5f99be501cd?w=600", "created_by": "system"},
  {"id": "ex-046", "name": "Curl Martillo", "category": "hipertrofia", "muscle_group": "biceps", "muscle_groups": ["biceps", "braquiorradial"], "equipment": "Mancuernas", "difficulty": "beginner", "description": "Curl con agarre neutro. Activa braquiorradial y bíceps conjunto.", "tips": ["Agarre neutro todo el tiempo", "Codos fijos", "Contracción completa"], "errors": ["Balancear", "Codos adelante", "Sin rango completo"], "tags": ["Bíceps", "Aislamiento"], "image": "https://images.unsplash.com/photo-1581009146145-b5ef050c2e1e?w=600", "created_by": "system"},
  {"id": "ex-047", "name": "Remo con Mancuerna", "category": "fuerza", "muscle_group": "espalda", "muscle_groups": ["espalda", "biceps"], "equipment": "Mancuernas", "difficulty": "beginner", "description": "Tracción unilateral. Alta carga de dorsal y romboides.", "tips": ["Apoyo en banco", "Codo pega al cuerpo", "Escápula al final"], "errors": ["Rotar el tronco", "Codo abierto", "Sin retracción"], "tags": ["Espalda", "Unilateral"], "image": "https://images.unsplash.com/photo-1597452485669-2c7bb5fef90d?w=600", "created_by": "system"},
  {"id": "ex-048", "name": "Press Arnold", "category": "hipertrofia", "muscle_group": "hombros", "muscle_groups": ["hombros", "triceps"], "equipment": "Mancuernas", "difficulty": "intermediate", "description": "Press de hombros con rotación. Mayor rango de movimiento.", "tips": ["Palmas al cuerpo al inicio", "Rotar mientras subís", "Extensión completa"], "errors": ["Sin rotación", "Espalda arqueada", "Demasiado peso"], "tags": ["Hombros", "Empuje"], "image": "https://images.unsplash.com/photo-1517838277536-f5f99be501cd?w=600", "created_by": "system"},
  {"id": "ex-049", "name": "Pull Over con Mancuerna", "category": "hipertrofia", "muscle_group": "espalda", "muscle_groups": ["espalda", "pecho", "serrato"], "equipment": "Mancuernas", "difficulty": "intermediate", "description": "Expansión torácica y trabajo de dorsal y serrato anterior.", "tips": ["Codos ligeramente flexionados", "Rango hasta sentir estiramiento", "Core activado"], "errors": ["Codos muy doblados", "Lumbar arqueada", "Sin rango completo"], "tags": ["Espalda", "Pecho"], "image": "https://images.unsplash.com/photo-1571019614242-c5c5dee9f50b?w=600", "created_by": "system"},
  {"id": "ex-050", "name": "Encogimientos de Trapecio", "category": "hipertrofia", "muscle_group": "trapecio", "muscle_groups": ["trapecio"], "equipment": "Mancuernas, Barra", "difficulty": "beginner", "description": "Aislamiento del trapecio superior.", "tips": ["Elevar verticalmente", "Pausa arriba", "No rotar los hombros"], "errors": ["Rotar hacia adelante", "Sin pausa arriba", "Demasiado peso"], "tags": ["Trapecio", "Aislamiento"], "image": "https://images.unsplash.com/photo-1517838277536-f5f99be501cd?w=600", "created_by": "system"},
  {"id": "ex-051", "name": "Curl Bíceps Barra EZ", "category": "hipertrofia", "muscle_group": "biceps", "muscle_groups": ["biceps"], "equipment": "Barra", "difficulty": "beginner", "description": "Curl con barra EZ. Menor estrés en muñecas que barra recta.", "tips": ["Codos fijos", "Agarre semisupinado", "Contracción completa"], "errors": ["Balancear el cuerpo", "Codos adelante", "Rango parcial"], "tags": ["Bíceps", "Aislamiento"], "image": "https://images.unsplash.com/photo-1581009146145-b5ef050c2e1e?w=600", "created_by": "system"},
  {"id": "ex-052", "name": "Skull Crusher", "category": "hipertrofia", "muscle_group": "triceps", "muscle_groups": ["triceps"], "equipment": "Barra, Mancuernas", "difficulty": "intermediate", "description": "Máximo estiramiento de cabeza larga del tríceps.", "tips": ["Codos apuntando al techo", "Bajar a la frente", "Control total"], "errors": ["Codos abiertos", "Bajar al cuello", "Demasiado peso"], "tags": ["Tríceps", "Aislamiento"], "image": "https://images.unsplash.com/photo-1571019614242-c5c5dee9f50b?w=600", "created_by": "system"},
  {"id": "ex-053", "name": "Remo Landmine", "category": "fuerza", "muscle_group": "espalda", "muscle_groups": ["espalda", "biceps", "core"], "equipment": "Barra", "difficulty": "intermediate", "description": "Remo con barra en landmine. Alta activación unilateral con estabilidad.", "tips": ["Cadera empuja atrás", "Tracción hacia cadera", "Core rígido"], "errors": ["Rotar el tronco", "Codo abierto", "Caderas elevadas"], "tags": ["Espalda", "Funcional"], "image": "https://images.unsplash.com/photo-1597452485669-2c7bb5fef90d?w=600", "created_by": "system"},
  {"id": "ex-054", "name": "Press Inclinado Agarre Neutro", "category": "hipertrofia", "muscle_group": "pecho", "muscle_groups": ["pecho", "triceps"], "equipment": "Mancuernas", "difficulty": "intermediate", "description": "Pectoral superior con menor estrés en hombro.", "tips": ["Agarre neutro", "Banco a 30°", "Codos a 45°"], "errors": ["Banco muy inclinado", "Codos a 90°", "Sin rango completo"], "tags": ["Pecho", "Empuje"], "image": "https://images.unsplash.com/photo-1571019614242-c5c5dee9f50b?w=600", "created_by": "system"},
  {"id": "ex-055", "name": "Band Pull Apart", "category": "prevencion", "muscle_group": "hombros", "muscle_groups": ["romboides", "trapecio medio", "rotadores"], "equipment": "Bandas Elásticas", "difficulty": "beginner", "description": "Apertura de banda a la altura de hombros. Salud postural y del hombro.", "tips": ["Brazos paralelos al suelo", "Apertura completa", "Escápulas juntas al final"], "errors": ["Codos flexionados", "Sin apertura completa", "Hombros encogidos"], "tags": ["Prevención", "Hombro", "Postura"], "image": "https://images.unsplash.com/photo-1517838277536-f5f99be501cd?w=600", "created_by": "system"},
  {"id": "ex-056", "name": "Plancha Lateral", "category": "core", "muscle_group": "oblicuos", "muscle_groups": ["oblicuos", "core", "abductores"], "equipment": "Peso Corporal", "difficulty": "intermediate", "description": "Isométrico lateral. Alta activación de oblicuos y cuadrado lumbar.", "tips": ["Cadera elevada", "Cuerpo alineado", "Respiración controlada"], "errors": ["Cadera caída", "Tronco rotado", "Cuello tenso"], "tags": ["Core", "Lateral", "Isométrico"], "image": "https://images.unsplash.com/photo-1571019614242-c5c5dee9f50b?w=600", "created_by": "system"},
  {"id": "ex-057", "name": "Hollow Body Hold", "category": "core", "muscle_group": "core", "muscle_groups": ["core", "psoas"], "equipment": "Peso Corporal", "difficulty": "intermediate", "description": "Isométrico fundamental de gimnasia. Activa core profundo.", "tips": ["Lumbar en el suelo", "Brazos extendidos", "Piernas bajas sin despegar lumbar"], "errors": ["Lumbar arqueada", "Cuello tenso", "Piernas muy bajas"], "tags": ["Core", "Isométrico", "Funcional"], "image": "https://images.unsplash.com/photo-1571019614242-c5c5dee9f50b?w=600", "created_by": "system"},
  {"id": "ex-058", "name": "Pallof Press", "category": "core", "muscle_group": "core", "muscle_groups": ["core", "oblicuos"], "equipment": "Poleas, Bandas Elásticas", "difficulty": "intermediate", "description": "Anti-rotación de core con polea. Fundamental para deportes de raqueta.", "tips": ["Pies a ancho de caderas", "Extender y retornar lentamente", "Core rígido todo el tiempo"], "errors": ["Rotar el tronco", "Impulso con brazos", "Demasiado peso"], "tags": ["Core", "Anti-rotación", "Padel"], "image": "https://images.unsplash.com/photo-1571019614242-c5c5dee9f50b?w=600", "created_by": "system"},
  {"id": "ex-059", "name": "Bird Dog", "category": "core", "muscle_group": "core", "muscle_groups": ["core", "gluteos", "espalda"], "equipment": "Peso Corporal", "difficulty": "beginner", "description": "Control motor contralateral. Estabilidad lumbo-pélvica.", "tips": ["Columna neutral", "Movimiento lento", "Exhalá al extender"], "errors": ["Rotar la cadera", "Lumbar arqueada", "Apoyar sin control"], "tags": ["Core", "Estabilidad"], "image": "https://images.unsplash.com/photo-1571019614242-c5c5dee9f50b?w=600", "created_by": "system"},
  {"id": "ex-060", "name": "Crunch en Polea Alta", "category": "hipertrofia", "muscle_group": "core", "muscle_groups": ["core"], "equipment": "Poleas", "difficulty": "beginner", "description": "Flexión de tronco con resistencia en polea.", "tips": ["Rodillas en el suelo", "Flexionar desde el ombligo", "Pausa abajo"], "errors": ["Usar cadera", "Demasiado peso", "Sin rango completo"], "tags": ["Core", "Hipertrofia"], "image": "https://images.unsplash.com/photo-1571019614242-c5c5dee9f50b?w=600", "created_by": "system"},
  {"id": "ex-061", "name": "Dragon Flag", "category": "core", "muscle_group": "core", "muscle_groups": ["core", "espalda", "gluteos"], "equipment": "Banco", "difficulty": "advanced", "description": "Ejercicio extremo de core. Activa la cadena posterior completa.", "tips": ["Bajar despacio", "Cuerpo rígido como tabla", "Apoyar en hombros"], "errors": ["Doblar caderas", "Caída descontrolada", "Sin progresión previa"], "tags": ["Core", "Avanzado"], "image": "https://images.unsplash.com/photo-1571019614242-c5c5dee9f50b?w=600", "created_by": "system"},
  {"id": "ex-062", "name": "Rotación de Tronco con Polea", "category": "core", "muscle_group": "oblicuos", "muscle_groups": ["oblicuos", "core"], "equipment": "Poleas", "difficulty": "intermediate", "description": "Rotación funcional con carga. Directamente aplicable al golpe en padel.", "tips": ["Rotar desde caderas", "Pies fijos", "Movimiento fluido"], "errors": ["Solo rotar los brazos", "Pies elevados", "Demasiado peso"], "tags": ["Core", "Rotacional", "Padel"], "image": "https://images.unsplash.com/photo-1571019614242-c5c5dee9f50b?w=600", "created_by": "system"},
  {"id": "ex-063", "name": "Levantamiento Turco", "category": "funcional", "muscle_group": "core", "muscle_groups": ["core", "hombros", "cuadriceps", "gluteos"], "equipment": "Pesa Rusa", "difficulty": "advanced", "description": "Movimiento complejo que integra todo el cuerpo. Alta demanda de coordinación.", "tips": ["Mirar la pesa todo el tiempo", "Movimiento lento", "Aprender sin peso primero"], "errors": ["Velocidad excesiva", "Perder el brazo extendido", "Sin progresión"], "tags": ["Funcional", "Kettlebell", "Core"], "image": "https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=600", "created_by": "system"},
  {"id": "ex-064", "name": "Swing con Pesa Rusa", "category": "potencia", "muscle_group": "gluteos", "muscle_groups": ["gluteos", "isquiotibiales", "core", "hombros"], "equipment": "Pesa Rusa", "difficulty": "intermediate", "description": "Bisagra de cadera explosiva. Desarrolla potencia y cardio simultáneamente.", "tips": ["Bisagra de cadera no sentadilla", "Explosión de caderas", "Freno activo abajo"], "errors": ["Sentadilla en lugar de bisagra", "Brazos tiran", "Espalda redondeada"], "tags": ["Kettlebell", "Potencia", "Cardio"], "image": "https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=600", "created_by": "system"},
  {"id": "ex-065", "name": "Goblet Squat Pesa Rusa", "category": "fuerza", "muscle_group": "cuadriceps", "muscle_groups": ["cuadriceps", "gluteos", "core"], "equipment": "Pesa Rusa", "difficulty": "beginner", "description": "Sentadilla frontal con pesa rusa. Enseña mecánica perfecta.", "tips": ["Codos debajo de la pesa", "Pecho erguido", "Talones abajo"], "errors": ["Inclinarse adelante", "Rodillas adentro", "Lumbar redondeada"], "tags": ["Kettlebell", "Tren Inferior"], "image": "https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=600", "created_by": "system"},
  {"id": "ex-066", "name": "Clean con Pesa Rusa", "category": "potencia", "muscle_group": "cuerpo_completo", "muscle_groups": ["gluteos", "hombros", "core", "biceps"], "equipment": "Pesa Rusa", "difficulty": "advanced", "description": "Movimiento olímpico con pesa rusa. Explosión de cadera a posición rack.", "tips": ["Codo pega al cuerpo", "Explosión de caderas", "Muñeca rota al final"], "errors": ["Tirar con el brazo", "Sin explosión de cadera", "Golpear la muñeca"], "tags": ["Kettlebell", "Potencia", "Olímpico"], "image": "https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=600", "created_by": "system"},
  {"id": "ex-067", "name": "Press con Pesa Rusa", "category": "fuerza", "muscle_group": "hombros", "muscle_groups": ["hombros", "triceps", "core"], "equipment": "Pesa Rusa", "difficulty": "intermediate", "description": "Press vertical unilateral. Alta demanda de estabilización de core.", "tips": ["Posición rack estable", "Core apretado", "Extensión completa"], "errors": ["Compensar con tronco", "Sin estabilidad en rack", "Codo adelante"], "tags": ["Kettlebell", "Hombros"], "image": "https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=600", "created_by": "system"},
  {"id": "ex-068", "name": "Snatch con Pesa Rusa", "category": "potencia", "muscle_group": "cuerpo_completo", "muscle_groups": ["gluteos", "hombros", "core"], "equipment": "Pesa Rusa", "difficulty": "advanced", "description": "El rey del kettlebell. Potencia cardio y coordinación en un solo movimiento.", "tips": ["Explosión de caderas", "Codo se flexiona al pasar", "Extensión arriba completa"], "errors": ["Tirar con el brazo", "Golpear muñeca", "Sin explosión"], "tags": ["Kettlebell", "Potencia", "Avanzado"], "image": "https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=600", "created_by": "system"},
  {"id": "ex-069", "name": "Farmer Carry", "category": "funcional", "muscle_group": "core", "muscle_groups": ["core", "trapecio", "antebrazos", "gluteos"], "equipment": "Mancuernas, Pesa Rusa", "difficulty": "beginner", "description": "Caminata con carga. Fuerza de agarre y estabilidad de core.", "tips": ["Hombros atrás y abajo", "Pasos cortos y controlados", "Core activado"], "errors": ["Hombros encogidos", "Pasos largos", "Tronco inclinado"], "tags": ["Funcional", "Agarre", "Core"], "image": "https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=600", "created_by": "system"},
  {"id": "ex-070", "name": "Suitcase Carry", "category": "funcional", "muscle_group": "core", "muscle_groups": ["core", "oblicuos", "cuadrado_lumbar"], "equipment": "Mancuernas, Pesa Rusa", "difficulty": "intermediate", "description": "Caminata unilateral. Alta activación antiflexión lateral.", "tips": ["No inclinar hacia el peso", "Core anti-lateral", "Pasos estables"], "errors": ["Inclinar el tronco", "Pasos rápidos", "Sin core activado"], "tags": ["Funcional", "Core", "Antiflexión"], "image": "https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=600", "created_by": "system"},
  {"id": "ex-071", "name": "Thruster con Mancuernas", "category": "funcional", "muscle_group": "cuerpo_completo", "muscle_groups": ["cuadriceps", "gluteos", "hombros", "triceps"], "equipment": "Mancuernas", "difficulty": "intermediate", "description": "Sentadilla + press en un movimiento. Muy metabólico.", "tips": ["Explosión de la sentadilla", "Inercia al press", "Core apretado"], "errors": ["Separar los movimientos", "Sin profundidad", "Caída incontrolada"], "tags": ["Funcional", "HIIT", "Metabólico"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-072", "name": "Turkish Get Up Simplificado", "category": "funcional", "muscle_group": "core", "muscle_groups": ["core", "hombros", "cuadriceps"], "equipment": "Pesa Rusa, Mancuernas", "difficulty": "intermediate", "description": "Versión simplificada del TGU. Integración completa.", "tips": ["Mirar la pesa", "Un paso a la vez", "Aprender sin peso"], "errors": ["Velocidad excesiva", "Perder el brazo", "Sin práctica progresiva"], "tags": ["Kettlebell", "Funcional"], "image": "https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=600", "created_by": "system"},
  {"id": "ex-073", "name": "Swing Americano", "category": "potencia", "muscle_group": "cuerpo_completo", "muscle_groups": ["gluteos", "isquiotibiales", "hombros", "core"], "equipment": "Pesa Rusa", "difficulty": "intermediate", "description": "Swing hasta overhead. Mayor rango que el swing ruso.", "tips": ["Extensión de cadera completa", "Brazos activos overhead", "Core al final"], "errors": ["Sin extensión de cadera", "Tirar con brazos", "Pérdida de control"], "tags": ["Kettlebell", "Potencia"], "image": "https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=600", "created_by": "system"},
  {"id": "ex-074", "name": "Depth Jump", "category": "potencia", "muscle_group": "cuadriceps", "muscle_groups": ["cuadriceps", "gluteos", "gemelos"], "equipment": "Cajón pliométrico", "difficulty": "advanced", "description": "Caída + salto inmediato. Máximo desarrollo de potencia reactiva.", "tips": ["Contacto mínimo en el suelo", "Aterrizaje suave", "Comenzar bajo 20-30cm"], "errors": ["Contacto prolongado", "Rodillas en valgo", "Altura excesiva al inicio"], "tags": ["Potencia", "Pliométrico", "Avanzado"], "image": "https://images.unsplash.com/photo-1526506118085-60ce8714f8c5?w=600", "created_by": "system"},
  {"id": "ex-075", "name": "Salto Vertical Máximo", "category": "potencia", "muscle_group": "cuadriceps", "muscle_groups": ["cuadriceps", "gluteos", "gemelos"], "equipment": "Peso Corporal", "difficulty": "intermediate", "description": "Salto máximo vertical. Medición y desarrollo de potencia explosiva.", "tips": ["Brazos hacia atrás", "Máxima extensión", "Aterrizaje en semiflexión"], "errors": ["Sin ayuda de brazos", "Aterrizaje rígido", "Rodillas en valgo"], "tags": ["Potencia", "Salto"], "image": "https://images.unsplash.com/photo-1526506118085-60ce8714f8c5?w=600", "created_by": "system"},
  {"id": "ex-076", "name": "Squat Jump", "category": "potencia", "muscle_group": "cuadriceps", "muscle_groups": ["cuadriceps", "gluteos"], "equipment": "Peso Corporal", "difficulty": "intermediate", "description": "Sentadilla explosiva con salto. Base del entrenamiento pliométrico.", "tips": ["Profundidad adecuada", "Máxima explosión", "Aterrizaje suave"], "errors": ["Profundidad insuficiente", "Aterrizaje rígido", "Rodillas en valgo"], "tags": ["Potencia", "Pliométrico"], "image": "https://images.unsplash.com/photo-1526506118085-60ce8714f8c5?w=600", "created_by": "system"},
  {"id": "ex-077", "name": "Triple Salto", "category": "potencia", "muscle_group": "cuadriceps", "muscle_groups": ["cuadriceps", "gluteos", "gemelos"], "equipment": "Peso Corporal", "difficulty": "advanced", "description": "Secuencia de tres saltos. Desarrolla potencia horizontal y coordinación.", "tips": ["Primer salto con dos piernas", "Segundo con una", "Aterrizaje estable"], "errors": ["Pérdida de impulso", "Aterrizaje brusco", "Sin coordinación"], "tags": ["Potencia", "Horizontal", "Pliométrico"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-078", "name": "Salto Lateral al Cajón", "category": "potencia", "muscle_group": "gluteos", "muscle_groups": ["gluteos", "abductores", "cuadriceps"], "equipment": "Cajón pliométrico", "difficulty": "intermediate", "description": "Salto lateral al cajón. Simula desplazamientos laterales de padel.", "tips": ["Empuje desde pierna externa", "Aterrizaje suave", "Bajar de frente"], "errors": ["Aterrizaje en valgo", "Caída del tronco", "Muy lejos del cajón"], "tags": ["Potencia", "Lateral", "Padel"], "image": "https://images.unsplash.com/photo-1526506118085-60ce8714f8c5?w=600", "created_by": "system"},
  {"id": "ex-079", "name": "Skipping Alto", "category": "cardio", "muscle_group": "cuadriceps", "muscle_groups": ["cuadriceps", "gemelos", "core"], "equipment": "Peso Corporal", "difficulty": "intermediate", "description": "Carrera elevando rodillas. Coordinación y cardio de alta intensidad.", "tips": ["Rodillas al nivel de caderas", "Brazos coordinados", "Punta de pies al aterrizar"], "errors": ["Tronco inclinado", "Sin coordinación de brazos", "Talones al aterrizar"], "tags": ["Cardio", "Coordinación", "HIIT"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-080", "name": "Salto con Tijera", "category": "cardio", "muscle_group": "cuadriceps", "muscle_groups": ["cuadriceps", "gluteos", "gemelos"], "equipment": "Peso Corporal", "difficulty": "intermediate", "description": "Zancada explosiva alternada con salto. Cardio + potencia.", "tips": ["Aterrizaje suave", "Tronco erguido", "Alternar piernas en el aire"], "errors": ["Aterrizaje rígido", "Tronco inclinado", "Rodilla en valgo"], "tags": ["Potencia", "Cardio", "HIIT"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-081", "name": "Lanzamiento Balón Medicinal", "category": "potencia", "muscle_group": "core", "muscle_groups": ["core", "hombros", "cuadriceps", "gluteos"], "equipment": "Pelota Medicinal", "difficulty": "intermediate", "description": "Lanzamiento rotacional. Transferencia directa a golpes de padel.", "tips": ["Rotar desde caderas", "Extensión completa", "Acompañar con el cuerpo"], "errors": ["Solo rotar hombros", "Sin extensión", "Pelota demasiado pesada"], "tags": ["Potencia", "Rotacional", "Padel"], "image": "https://images.unsplash.com/photo-1622279457486-62dcc4a431d6?w=600", "created_by": "system"},
  {"id": "ex-082", "name": "Salto Profundidad Lateral", "category": "potencia", "muscle_group": "gluteos", "muscle_groups": ["gluteos", "abductores", "gemelos"], "equipment": "Peso Corporal", "difficulty": "advanced", "description": "Caída lateral + salto lateral inmediato. Reactivo lateral para padel.", "tips": ["Mínimo contacto", "Empuje inmediato", "Control en aterrizaje"], "errors": ["Contacto prolongado", "Sin explosión", "Rodilla en valgo"], "tags": ["Potencia", "Reactivo", "Padel"], "image": "https://images.unsplash.com/photo-1526506118085-60ce8714f8c5?w=600", "created_by": "system"},
  {"id": "ex-083", "name": "Apertura Cadera Cuadrupedia", "category": "movilidad", "muscle_group": "gluteos", "muscle_groups": ["gluteos", "rotadores de cadera"], "equipment": "Peso Corporal", "difficulty": "beginner", "description": "Rotación de cadera en cuadrupedia. Activa glúteo y gana rango rotacional.", "tips": ["Cadera fija", "Movimiento circular amplio", "Respirar con el movimiento"], "errors": ["Cadera se mueve", "Radio pequeño", "Velocidad excesiva"], "tags": ["Movilidad", "Cadera", "Calentamiento"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-084", "name": "Movilidad de Tobillo", "category": "movilidad", "muscle_group": "gemelos", "muscle_groups": ["gemelos", "tobillo"], "equipment": "Peso Corporal", "difficulty": "beginner", "description": "Flexión dorsal de tobillo. Clave para sentadilla profunda y desplazamientos.", "tips": ["Rodilla sobre el pie", "Talón en el suelo", "Avanzar progresivamente"], "errors": ["Talón levantado", "Exceso de pronación", "Sin progresión"], "tags": ["Movilidad", "Tobillo", "Prevención"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-085", "name": "Rotación Torácica", "category": "movilidad", "muscle_group": "espalda", "muscle_groups": ["espalda", "oblicuos"], "equipment": "Peso Corporal", "difficulty": "beginner", "description": "Movilidad torácica. Fundamental para golpes de padel y postura.", "tips": ["Lumbar fija", "Llevar codo al cielo", "Respirar abriendo el pecho"], "errors": ["Rotar desde lumbar", "Sin rango completo", "Velocidad excesiva"], "tags": ["Movilidad", "Torácica", "Padel"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-086", "name": "Estiramiento Cadera en Z", "category": "movilidad", "muscle_group": "gluteos", "muscle_groups": ["gluteos", "piriforme", "rotadores"], "equipment": "Peso Corporal", "difficulty": "beginner", "description": "Movilidad de rotadores de cadera. Previene lesiones en el tren inferior.", "tips": ["Ambas piernas a 90°", "No forzar", "Respiración profunda"], "errors": ["Compensar con lumbar", "Apurarse", "Piernas no a 90°"], "tags": ["Movilidad", "Cadera", "Prevención"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-087", "name": "RDL con Banda", "category": "movilidad", "muscle_group": "isquiotibiales", "muscle_groups": ["isquiotibiales", "espalda"], "equipment": "Bandas Elásticas", "difficulty": "beginner", "description": "Estiramiento activo de isquiotibiales con resistencia.", "tips": ["Bisagra de cadera", "Columna neutral", "Lentamente"], "errors": ["Redondear espalda", "Rodillas flexionadas", "Velocidad excesiva"], "tags": ["Movilidad", "Isquiotibiales"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-088", "name": "Gato-Camello", "category": "movilidad", "muscle_group": "espalda", "muscle_groups": ["espalda", "core"], "equipment": "Peso Corporal", "difficulty": "beginner", "description": "Movilidad de columna lumbar y torácica. Calentamiento básico de espalda.", "tips": ["Movimiento vertebra a vertebra", "Sincronizar con respiración", "Sin brusquedad"], "errors": ["Movimiento de golpe", "Sin respiración", "Rango pequeño"], "tags": ["Movilidad", "Espalda", "Calentamiento"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-089", "name": "Lunges con Rotación", "category": "movilidad", "muscle_group": "cadera", "muscle_groups": ["cadera", "oblicuos", "cuadriceps"], "equipment": "Peso Corporal", "difficulty": "beginner", "description": "Zancada con rotación torácica. Calentamiento funcional completo.", "tips": ["Rotar hacia la pierna delantera", "Cadera baja", "Tronco erguido"], "errors": ["Sin rotación", "Rodilla en valgo", "Tronco inclinado"], "tags": ["Movilidad", "Calentamiento", "Funcional"], "image": "https://images.unsplash.com/photo-1581009146145-b5ef050c2e1e?w=600", "created_by": "system"},
  {"id": "ex-090", "name": "Inchworm", "category": "movilidad", "muscle_group": "isquiotibiales", "muscle_groups": ["isquiotibiales", "espalda", "hombros", "core"], "equipment": "Peso Corporal", "difficulty": "beginner", "description": "Caminata de manos. Movilidad de isquios + activación de hombros.", "tips": ["Piernas rectas al caminar", "Plancha al llegar", "Volver controlado"], "errors": ["Doblar rodillas", "Cadera alta en plancha", "Demasiado rápido"], "tags": ["Movilidad", "Calentamiento", "Full Body"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-091", "name": "Foam Roller Cuádriceps", "category": "movilidad", "muscle_group": "cuadriceps", "muscle_groups": ["cuadriceps"], "equipment": "Foam Roller", "difficulty": "beginner", "description": "Liberación miofascial de cuádriceps. Recuperación y movilidad.", "tips": ["Peso graduado", "Pausa en puntos dolorosos", "Respiración profunda"], "errors": ["Rodar demasiado rápido", "Demasiado peso", "Sin pausa"], "tags": ["Recuperación", "Movilidad"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-092", "name": "Foam Roller Espalda Alta", "category": "movilidad", "muscle_group": "espalda", "muscle_groups": ["espalda", "torácica"], "equipment": "Foam Roller", "difficulty": "beginner", "description": "Liberación y extensión torácica con foam roller. Mejora postura y movilidad de hombros.", "tips": ["Manos detrás de la cabeza", "Extender sobre el roller", "Vertebra a vertebra"], "errors": ["Rodar sobre lumbar", "Sin control", "Demasiado rápido"], "tags": ["Movilidad", "Torácica", "Recuperación"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-093", "name": "Mountain Climbers", "category": "cardio", "muscle_group": "core", "muscle_groups": ["core", "cuadriceps", "hombros"], "equipment": "Peso Corporal", "difficulty": "intermediate", "description": "Cardio + core en plancha. Alta intensidad cardiovascular.", "tips": ["Cadera estable", "Rodillas al pecho", "Velocidad progresiva"], "errors": ["Cadera arriba", "Golpe en el suelo", "Sin activación de core"], "tags": ["Cardio", "Core", "HIIT"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-094", "name": "Jumping Jacks", "category": "cardio", "muscle_group": "cuerpo_completo", "muscle_groups": ["gemelos", "hombros", "core"], "equipment": "Peso Corporal", "difficulty": "beginner", "description": "Clásico de calentamiento y cardio.", "tips": ["Aterrizaje suave", "Brazos coordinados", "Ritmo constante"], "errors": ["Aterrizaje rígido", "Sin coordinación", "Muy lento"], "tags": ["Cardio", "Calentamiento"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-095", "name": "Sprint en Estático", "category": "cardio", "muscle_group": "cuadriceps", "muscle_groups": ["cuadriceps", "gemelos", "core"], "equipment": "Peso Corporal", "difficulty": "intermediate", "description": "Carrera en estático a máxima velocidad. Cardio explosivo.", "tips": ["Rodillas altas", "Brazos coordinados", "Inclinación leve adelante"], "errors": ["Rodillas bajas", "Sin coordinación de brazos", "Caída del tronco"], "tags": ["Cardio", "HIIT", "Velocidad"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-096", "name": "Bear Crawl", "category": "funcional", "muscle_group": "core", "muscle_groups": ["core", "hombros", "cuadriceps"], "equipment": "Peso Corporal", "difficulty": "intermediate", "description": "Movimiento de cuadrupedia dinámico. Integración completa.", "tips": ["Rodillas a 2cm del suelo", "Patrón contralateral", "Core activado"], "errors": ["Rodillas tocan el suelo", "Patrón ipsilateral", "Cadera alta"], "tags": ["Funcional", "Core", "Cardio"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-097", "name": "Battle Ropes Ondas", "category": "cardio", "muscle_group": "hombros", "muscle_groups": ["hombros", "core", "antebrazos"], "equipment": "Cuerdas de Batalla", "difficulty": "intermediate", "description": "Ondas con cuerdas de batalla. Cardio de tren superior muy intenso.", "tips": ["Rodillas flexionadas", "Ondas amplias", "Respiración controlada"], "errors": ["Rodillas rectas", "Ondas pequeñas", "Sin continuidad"], "tags": ["Cardio", "HIIT", "Tren Superior"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-098", "name": "Saltos de Cuerda Doble", "category": "cardio", "muscle_group": "gemelos", "muscle_groups": ["gemelos", "core", "hombros"], "equipment": "Cuerda de salto", "difficulty": "advanced", "description": "Double unders. Coordinación y cardio de alta intensidad.", "tips": ["Salto más alto", "Muñecas rápidas", "Ritmo estable"], "errors": ["Salto insuficiente", "Muñecas lentas", "Sin ritmo"], "tags": ["Cardio", "Coordinación", "Avanzado"], "image": "https://images.unsplash.com/photo-1581009146145-b5ef050c2e1e?w=600", "created_by": "system"},
  {"id": "ex-099", "name": "Sentadilla con Banda", "category": "prevencion", "muscle_group": "gluteos", "muscle_groups": ["gluteos", "abductores", "cuadriceps"], "equipment": "Bandas Elásticas", "difficulty": "beginner", "description": "Sentadilla con banda en rodillas. Activa glúteo medio y previene valgo.", "tips": ["Empujar la banda hacia afuera", "Rodillas sobre los pies", "Descenso controlado"], "errors": ["Rodillas cediendo", "Band muy floja", "Sin activación de glúteo medio"], "tags": ["Prevención", "Rodilla", "Glúteo Medio"], "image": "https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=600", "created_by": "system"},
  {"id": "ex-100", "name": "Clamshell", "category": "prevencion", "muscle_group": "gluteos", "muscle_groups": ["gluteo_medio", "rotadores"], "equipment": "Bandas Elásticas", "difficulty": "beginner", "description": "Apertura de cadera en lateral. Activa glúteo medio y piriforme.", "tips": ["Cadera no se mueve", "Apertura lenta", "Band en rodillas"], "errors": ["Mover la cadera", "Rango corto", "Velocidad excesiva"], "tags": ["Prevención", "Glúteo Medio"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-101", "name": "Hombro en Y-T-W", "category": "prevencion", "muscle_group": "hombros", "muscle_groups": ["trapecio medio", "romboides", "manguito"], "equipment": "Mancuernas, Bandas Elásticas", "difficulty": "beginner", "description": "Fortalece músculos estabilizadores de escápula. Salud del hombro.", "tips": ["Peso muy liviano", "Movimiento controlado", "Escápulas deprimidas"], "errors": ["Demasiado peso", "Encogimiento de hombros", "Velocidad excesiva"], "tags": ["Prevención", "Hombro", "Postura"], "image": "https://images.unsplash.com/photo-1517838277536-f5f99be501cd?w=600", "created_by": "system"},
  {"id": "ex-102", "name": "Single Leg RDL", "category": "prevencion", "muscle_group": "isquiotibiales", "muscle_groups": ["isquiotibiales", "gluteos", "core"], "equipment": "Peso Corporal, Mancuernas", "difficulty": "intermediate", "description": "Peso muerto unilateral. Equilibrio + fuerza + prevención de tobillos.", "tips": ["Cadera cuadrada", "Pie de apoyo ligeramente flexionado", "Core activado"], "errors": ["Cadera rotada", "Perder equilibrio por velocidad", "Redondear espalda"], "tags": ["Prevención", "Unilateral", "Equilibrio"], "image": "https://images.unsplash.com/photo-1597452485669-2c7bb5fef90d?w=600", "created_by": "system"},
  {"id": "ex-103", "name": "Elevación Excéntrica de Gemelos", "category": "prevencion", "muscle_group": "gemelos", "muscle_groups": ["gemelos", "soleo"], "equipment": "Escalón", "difficulty": "beginner", "description": "Excéntrico de gemelos. Previene y rehabilita lesiones de Aquiles.", "tips": ["Subir con dos piernas", "Bajar con una", "Rango completo abajo"], "errors": ["Sin rango excéntrico", "Solo concéntrico", "Demasiada velocidad"], "tags": ["Prevención", "Aquiles", "Gemelos"], "image": "https://images.unsplash.com/photo-1581009146145-b5ef050c2e1e?w=600", "created_by": "system"},
  {"id": "ex-104", "name": "Nordic Hamstring Curl", "category": "prevencion", "muscle_group": "isquiotibiales", "muscle_groups": ["isquiotibiales"], "equipment": "Banco", "difficulty": "advanced", "description": "Variante excéntrica de isquiotibiales. La más efectiva para prevención.", "tips": ["Bajar lo más lento posible", "Manos para frenar la caída", "Progresión: primero asistido"], "errors": ["Caída libre", "Sin control", "Sin progresión"], "tags": ["Prevención", "Isquiotibiales", "Excéntrico"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-105", "name": "Equilibrio Pie con Ojos Cerrados", "category": "prevencion", "muscle_group": "tobillo", "muscle_groups": ["tobillo", "core", "gemelos"], "equipment": "Peso Corporal", "difficulty": "beginner", "description": "Propiocepción de tobillo. Previene esguinces. Ideal para deportistas.", "tips": ["Punto de fijación al inicio", "Progresión: primero ojos abiertos", "Desafío: sobre superficie inestable"], "errors": ["Demasiado rápido a ojos cerrados", "Sin progresión", "Compensar con cadera"], "tags": ["Prevención", "Tobillo", "Propiocepción"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-106", "name": "Volea de Drive — Técnica", "category": "padel_specific", "muscle_group": "hombros", "muscle_groups": ["hombros", "antebrazo", "core"], "equipment": "Pala de Pádel", "difficulty": "beginner", "description": "Volea de drive con pala. Posición de pala alta, contacto frente al cuerpo, muñeca fija.", "tips": ["Pala arriba antes del golpe", "Contacto frente al cuerpo", "Muñeca bloqueada", "Paso hacia la pelota"], "errors": ["Pala baja en el impacto", "Golpear tarde", "Muñeca flexible", "Sin movimiento de piernas"], "tags": ["Pádel", "Volea", "Drive", "Técnico"], "image": "https://images.unsplash.com/photo-1622279457486-62dcc4a431d6?w=600", "created_by": "system"},
  {"id": "ex-107", "name": "Volea de Revés — Técnica", "category": "padel_specific", "muscle_group": "hombros", "muscle_groups": ["hombros", "antebrazo", "core"], "equipment": "Pala de Pádel", "difficulty": "beginner", "description": "Volea de revés con pala. Hombro contrario adelante, pala continental.", "tips": ["Hombro no dominante adelante", "Pala en agarre continental", "Golpe corto y firme", "Peso en el pie adelante"], "errors": ["Hombro dominante adelante", "Swing largo", "Muñeca rota", "Sin transferencia de peso"], "tags": ["Pádel", "Volea", "Revés", "Técnico"], "image": "https://images.unsplash.com/photo-1622279457486-62dcc4a431d6?w=600", "created_by": "system"},
  {"id": "ex-108", "name": "Bandeja — Técnica", "category": "padel_specific", "muscle_group": "hombros", "muscle_groups": ["hombros", "core", "triceps"], "equipment": "Pala de Pádel", "difficulty": "intermediate", "description": "Bandeja: golpe defensivo aéreo con efecto liftado hacia las paredes laterales.", "tips": ["Contacto sobre la cabeza", "Efecto liftado de adentro hacia afuera", "Ángulo de salida controlado", "Posición de red inmediata después"], "errors": ["Contacto demasiado atrás", "Sin efecto", "Dejar caer mucho la pelota", "No recuperar la red"], "tags": ["Pádel", "Bandeja", "Aéreo", "Técnico"], "image": "https://images.unsplash.com/photo-1622279457486-62dcc4a431d6?w=600", "created_by": "system"},
  {"id": "ex-109", "name": "Víbora — Técnica", "category": "padel_specific", "muscle_group": "hombros", "muscle_groups": ["hombros", "core", "antebrazo"], "equipment": "Pala de Pádel", "difficulty": "advanced", "description": "Víbora: variante agresiva de la bandeja con salida a las paredes laterales y bote difícil.", "tips": ["Giro del antebrazo al impacto", "Contacto lateral al cuerpo", "Dirección cruzada preferentemente", "Timing perfecto en el salto"], "errors": ["Sin pronación del antebrazo", "Contacto frontal", "Timing errado", "Pérdida de posición"], "tags": ["Pádel", "Víbora", "Aéreo", "Avanzado"], "image": "https://images.unsplash.com/photo-1622279457486-62dcc4a431d6?w=600", "created_by": "system"},
  {"id": "ex-110", "name": "Bajada Pared de Cristal — Técnica", "category": "padel_specific", "muscle_group": "core", "muscle_groups": ["core", "hombros", "piernas"], "equipment": "Pala de Pádel", "difficulty": "intermediate", "description": "Golpe tras el cristal trasero. Técnica de cuchara y salida rápida a la red.", "tips": ["Dejar que la pelota pase", "Cuchara con muñeca", "Salida rápida a la red", "Comunicación con el compañero"], "errors": ["Golpear antes de que pase", "Sin cuchara", "No salir a la red", "Golpe demasiado fuerte"], "tags": ["Pádel", "Cristal", "Defensa", "Técnico"], "image": "https://images.unsplash.com/photo-1622279457486-62dcc4a431d6?w=600", "created_by": "system"},
  {"id": "ex-111", "name": "Remate — Técnica", "category": "padel_specific", "muscle_group": "hombros", "muscle_groups": ["hombros", "core", "triceps"], "equipment": "Pala de Pádel", "difficulty": "advanced", "description": "Remate en pádel: golpe aéreo de ataque máximo sobre la pelota alta.", "tips": ["Girar hombros", "Pala atrás rápido", "Contacto en el punto máximo", "Seguimiento después del golpe"], "errors": ["Sin rotación de hombros", "Contacto tarde", "Apuntar siempre al mismo lado", "Sin seguimiento"], "tags": ["Pádel", "Remate", "Ataque", "Técnico"], "image": "https://images.unsplash.com/photo-1622279457486-62dcc4a431d6?w=600", "created_by": "system"},
  {"id": "ex-112", "name": "Globo — Técnica", "category": "padel_specific", "muscle_group": "hombros", "muscle_groups": ["hombros", "core"], "equipment": "Pala de Pádel", "difficulty": "intermediate", "description": "El globo: pelota alta para recuperar la red. Recurso defensivo fundamental.", "tips": ["Acompañar con el cuerpo", "Trayectoria alta y profunda", "Volver a posición de red", "Efecto liftado ayuda al control"], "errors": ["Globo corto", "Sin efecto", "No recuperar posición", "Globo plano fácil de remate"], "tags": ["Pádel", "Globo", "Defensa", "Técnico"], "image": "https://images.unsplash.com/photo-1622279457486-62dcc4a431d6?w=600", "created_by": "system"},
  {"id": "ex-113", "name": "Drive desde Fondo — Técnica", "category": "padel_specific", "muscle_group": "core", "muscle_groups": ["core", "hombros", "piernas"], "equipment": "Pala de Pádel", "difficulty": "intermediate", "description": "Drive desde el fondo de la pista con dirección cruzada o paralela.", "tips": ["Preparación temprana", "Transferencia de peso adelante", "Golpe liftado para control", "Dirección cruzada como opción principal"], "errors": ["Preparación tarde", "Sin transferencia", "Golpe plano al fondo", "Sin dirección"], "tags": ["Pádel", "Drive", "Fondo", "Técnico"], "image": "https://images.unsplash.com/photo-1622279457486-62dcc4a431d6?w=600", "created_by": "system"},
  {"id": "ex-114", "name": "Revés desde Fondo — Técnica", "category": "padel_specific", "muscle_group": "core", "muscle_groups": ["core", "hombros"], "equipment": "Pala de Pádel", "difficulty": "intermediate", "description": "Golpe de revés desde el fondo. Control y dirección cruzada.", "tips": ["Hombro no dominante adelante", "Acompañar con el cuerpo", "Efecto slice para control", "Profundidad sobre velocidad"], "errors": ["Sin rotación de hombros", "Muñeca rota", "Golpe plano al fondo", "Sin slice"], "tags": ["Pádel", "Revés", "Fondo", "Técnico"], "image": "https://images.unsplash.com/photo-1622279457486-62dcc4a431d6?w=600", "created_by": "system"},
  {"id": "ex-115", "name": "Servicio — Técnica", "category": "padel_specific", "muscle_group": "hombros", "muscle_groups": ["hombros", "core", "triceps"], "equipment": "Pala de Pádel", "difficulty": "beginner", "description": "Saque en pádel. Debe ser por debajo de la cadera con bote previo.", "tips": ["Bote obligatorio", "Debajo de la cintura", "Dirigir a los pies del rival", "Variedad de efectos"], "errors": ["Muy previsible", "Sin efecto", "Muy largo", "Sin activar con el saque"], "tags": ["Pádel", "Saque", "Técnico"], "image": "https://images.unsplash.com/photo-1622279457486-62dcc4a431d6?w=600", "created_by": "system"},
  {"id": "ex-116", "name": "Dejada — Técnica", "category": "padel_specific", "muscle_group": "hombros", "muscle_groups": ["hombros", "muñeca"], "equipment": "Pala de Pádel", "difficulty": "advanced", "description": "Dejada: drop shot que cae corto y bota dos veces antes de la red.", "tips": ["Gesto de volea pero suave", "Contragolpe de muñeca al final", "Punto bajo de contacto", "Ejecutar desde red"], "errors": ["Golpe muy fuerte", "Sin contragolpe", "Pelota llega a la red rival", "Sin punto bajo"], "tags": ["Pádel", "Dejada", "Táctica", "Avanzado"], "image": "https://images.unsplash.com/photo-1622279457486-62dcc4a431d6?w=600", "created_by": "system"},
  {"id": "ex-117", "name": "Transferencia Rotación Elástico", "category": "transferencia", "muscle_group": "core", "muscle_groups": ["core", "oblicuos", "hombros"], "equipment": "Bandas Elásticas", "difficulty": "intermediate", "description": "Simulación del gesto rotacional del golpe de pádel con banda elástica.", "tips": ["Pies fijos", "Rotar desde caderas", "Misma velocidad que el golpe real", "Seguimiento completo"], "errors": ["Solo rotar hombros", "Pies moviéndose", "Sin velocidad", "Rango corto"], "tags": ["Transferencia", "Pádel", "Rotacional"], "image": "https://images.unsplash.com/photo-1622279457486-62dcc4a431d6?w=600", "created_by": "system"},
  {"id": "ex-118", "name": "Transferencia Swing con Palo", "category": "transferencia", "muscle_group": "core", "muscle_groups": ["core", "hombros", "oblicuos"], "equipment": "Palo, Pica", "difficulty": "beginner", "description": "Gesto técnico del swing de drive y revés con palo. Mecánica pura sin pelota.", "tips": ["Foco en la mecánica", "Ritmo bajo primero", "Activar las caderas primero", "Mirar al frente"], "errors": ["Solo usar brazos", "Velocidad excesiva al inicio", "Sin activación de caderas", "Mirar la mano"], "tags": ["Transferencia", "Pádel", "Técnico"], "image": "https://images.unsplash.com/photo-1622279457486-62dcc4a431d6?w=600", "created_by": "system"},
  {"id": "ex-119", "name": "Transferencia Desplazamiento Lateral", "category": "transferencia", "muscle_group": "gluteos", "muscle_groups": ["gluteos", "cuadriceps", "abductores"], "equipment": "Conos", "difficulty": "intermediate", "description": "Desplazamiento lateral entre conos simulando movimiento en la cancha.", "tips": ["Split step previo", "Paso cruzado si está lejos", "Recuperación al centro", "Pies activos"], "errors": ["Sin split step", "Cruzar siempre", "No recuperar", "Pasos largos lentos"], "tags": ["Transferencia", "Pádel", "Velocidad"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-120", "name": "Transferencia Entrada a la Red", "category": "transferencia", "muscle_group": "cuadriceps", "muscle_groups": ["cuadriceps", "gluteos", "core"], "equipment": "Conos", "difficulty": "intermediate", "description": "Sprint corto a la red + posición de volea. Simula el avance post-globo rival.", "tips": ["Tres pasos explosivos", "Freno activo en la red", "Split step al llegar", "Pala arriba al frenar"], "errors": ["Avance sin freno", "Pala abajo al llegar", "Sin split step", "Demasiados pasos"], "tags": ["Transferencia", "Pádel", "Táctica"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-121", "name": "Escalera Agilidad Pasos Laterales", "category": "padel_specific", "muscle_group": "gemelos", "muscle_groups": ["gemelos", "cuadriceps", "core"], "equipment": "Escalera de Agilidad", "difficulty": "beginner", "description": "Pasos laterales en escalera. Coordinación de pies para pádel.", "tips": ["Punta de pies", "Brazos coordinados", "Mirar adelante", "Velocidad progresiva"], "errors": ["Talones", "Sin coordinación de brazos", "Mirar los pies", "Demasiado rápido al inicio"], "tags": ["Pádel", "Agilidad", "Coordinación"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-122", "name": "Escalera Agilidad In-Out", "category": "padel_specific", "muscle_group": "gemelos", "muscle_groups": ["gemelos", "cuadriceps"], "equipment": "Escalera de Agilidad", "difficulty": "intermediate", "description": "Patrón in-out en escalera. Coordinación avanzada de pies.", "tips": ["Patrón primero lento", "Punta de pies", "Ritmo constante", "Brazos coordinados"], "errors": ["Pisar la escalera", "Sin ritmo", "Mirar los pies", "Sin coordinación de brazos"], "tags": ["Pádel", "Agilidad", "Coordinación"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-123", "name": "T-Drill de Agilidad", "category": "padel_specific", "muscle_group": "cuadriceps", "muscle_groups": ["cuadriceps", "gluteos", "gemelos"], "equipment": "Conos", "difficulty": "intermediate", "description": "Patrón en T con conos. Cambios de dirección a máxima velocidad.", "tips": ["Sprint recto primero", "Paso cruzado en el lateral", "Agachar en los conos", "Máxima velocidad"], "errors": ["Sin agacharse", "Pasos largos en lateral", "Sin aceleración", "Sin desaceleración controlada"], "tags": ["Pádel", "Agilidad", "Cambio de Dirección"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-124", "name": "Shuttle Run 5-10-5", "category": "padel_specific", "muscle_group": "cuadriceps", "muscle_groups": ["cuadriceps", "gluteos", "gemelos"], "equipment": "Conos", "difficulty": "intermediate", "description": "Shuttle run 5-10-5. Cambia de dirección 180° dos veces. Fundamental para agilidad.", "tips": ["Arranque explosivo", "Freno bajo al girar", "Tocar el cono", "Sprint máximo"], "errors": ["Freno tardío", "Sin tocar el cono", "Sin explosión de salida", "Postura alta al girar"], "tags": ["Pádel", "Agilidad", "Velocidad"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-125", "name": "Voleo Concentración en Red", "category": "padel_specific", "muscle_group": "hombros", "muscle_groups": ["hombros", "antebrazo", "muñeca"], "equipment": "Pala de Pádel", "difficulty": "beginner", "description": "Volear a la pared desde corta distancia. Mejora reflejos y bloqueo de muñeca.", "tips": ["Muy cerca de la pared", "Ritmo constante", "Muñeca firme", "Pala alta siempre"], "errors": ["Demasiado lejos", "Sin ritmo", "Muñeca suelta", "Pala baja"], "tags": ["Pádel", "Reflejos", "Red"], "image": "https://images.unsplash.com/photo-1622279457486-62dcc4a431d6?w=600", "created_by": "system"},
  {"id": "ex-126", "name": "Reacción a Señal del Coach", "category": "transferencia", "muscle_group": "cuadriceps", "muscle_groups": ["cuadriceps", "gluteos", "gemelos"], "equipment": "Conos", "difficulty": "intermediate", "description": "Reacción y desplazamiento a señal del coach. Entrena tiempo de reacción para pádel.", "tips": ["Split step constante", "Reacción inmediata", "Recuperar siempre al centro", "Leer la pelota del coach"], "errors": ["Sin split step", "Anticipar", "No recuperar", "Pasos cruzados innecesarios"], "tags": ["Transferencia", "Pádel", "Reacción"], "image": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=600", "created_by": "system"},
  {"id": "ex-127", "name": "Ejercicio de Pies con Pelota", "category": "padel_specific", "muscle_group": "gemelos", "muscle_groups": ["gemelos", "cuadriceps", "core"], "equipment": "Pala de Pádel", "difficulty": "beginner", "description": "Bounce de pelota con pala mientras se realizan desplazamientos. Coordinación pala-pies.", "tips": ["Mirar la pelota y el campo", "Bounce continuo", "Variar velocidades", "Añadir desplazamientos"], "errors": ["Solo mirar la pelota", "Perder el bounce", "Sin desplazamiento", "Muy estático"], "tags": ["Pádel", "Coordinación", "Técnico"], "image": "https://images.unsplash.com/photo-1622279457486-62dcc4a431d6?w=600", "created_by": "system"},
  {"id": "ex-128", "name": "Posición Base y Split Step", "category": "padel_specific", "muscle_group": "gemelos", "muscle_groups": ["gemelos", "cuadriceps"], "equipment": "Peso Corporal", "difficulty": "beginner", "description": "Posición base de pádel y el split step sincronizado.", "tips": ["Peso en punta de pies", "Split step cuando el rival toca", "Manos delante del cuerpo", "Pala a la altura del pecho"], "errors": ["Talones en el suelo", "Sin split step", "Manos a los costados", "Pala baja"], "tags": ["Pádel", "Técnico", "Fundamental"], "image": "https://images.unsplash.com/photo-1622279457486-62dcc4a431d6?w=600", "created_by": "system"},
]

ROUTINES_SEED = [
  {"id":"rut-001","name":"Full Body A","description":"Cuerpo completo para principiantes e intermedios.",
   "type":"classic","difficulty":"intermediate","tags":["Full Body","Fuerza"],"circuit":None,"coach_id": "system",
   "exercises":[{"exerciseId":"ex-001","sets":3,"reps":"12","weight":"BW","restBetweenSets":60},{"exerciseId":"ex-006","sets":3,"reps":"10","weight":"40","restBetweenSets":90},{"exerciseId":"ex-009","sets":3,"reps":"6","weight":"BW","restBetweenSets":120},{"exerciseId":"ex-017","sets":3,"reps":"30s","weight":"","restBetweenSets":45},{"exerciseId":"ex-014","sets":3,"reps":"12","weight":"8","restBetweenSets":60}],
   "created_at":"2026-01-15T10:00:00"},
  {"id":"rut-002","name":"Padel Performance","description":"Potencia + técnica padel. Transferencia directa a la pista.",
   "type":"circuit","difficulty":"intermediate","tags":["Padel","Potencia"],"coach_id": "system",
   "circuit":{"rounds":3,"work":40,"rest_ex":15,"rest_round":60},
   "exercises":[{"exerciseId":"ex-022","weight":"","notes":"Altura máxima"},{"exerciseId":"ex-027","weight":"","notes":"8 por lado"},{"exerciseId":"ex-026","weight":"","notes":"15 cada lado"},{"exerciseId":"ex-028","weight":"","notes":"10 repeticiones"},{"exerciseId":"ex-017","weight":"","notes":"Mantener posición"}],
   "created_at":"2026-01-16T10:00:00"},
  {"id":"rut-003","name":"Cadena Posterior","description":"Glúteos, isquiotibiales y espalda baja.",
   "type":"classic","difficulty":"advanced","tags":["Piernas","Cadena Posterior"],"circuit":None,"coach_id": "system",
   "exercises":[{"exerciseId":"ex-002","sets":4,"reps":"6","weight":"80","restBetweenSets":180},{"exerciseId":"ex-004","sets":4,"reps":"10","weight":"60","restBetweenSets":90},{"exerciseId":"ex-003","sets":3,"reps":"8","weight":"20","restBetweenSets":90},{"exerciseId":"ex-018","sets":3,"reps":"10","weight":"","restBetweenSets":45}],
   "created_at":"2026-01-17T10:00:00"},
  {"id": "rut-004", "name": "Fuerza 5x5", "description": "Programa de fuerza máxima basado en los 5 ejercicios compuestos fundamentales.", "type": "1rm", "difficulty": "advanced", "tags": ["Fuerza", "5x5", "Compuestos"], "circuit": None, "coach_id": "system", "exercises": [{"exerciseId": "ex-001", "sets": 5, "reps": "5", "weight": "80", "restBetweenSets": 180, "setDetails": []}, {"exerciseId": "ex-006", "sets": 5, "reps": "5", "weight": "70", "restBetweenSets": 180, "setDetails": []}, {"exerciseId": "ex-002", "sets": 5, "reps": "5", "weight": "100", "restBetweenSets": 180, "setDetails": []}, {"exerciseId": "ex-009", "sets": 5, "reps": "5", "weight": "BW", "restBetweenSets": 120, "setDetails": []}, {"exerciseId": "ex-012", "sets": 5, "reps": "5", "weight": "50", "restBetweenSets": 180, "setDetails": []}], "created_at": "2026-01-20T10:00:00"},
  {"id": "rut-005", "name": "Tren Superior — Empuje/Tracción", "description": "Sesión completa de tren superior. Equilibrio perfecto entre empuje y tracción.", "type": "classic", "difficulty": "intermediate", "tags": ["Tren Superior", "Push/Pull"], "circuit": None, "coach_id": "system", "exercises": [{"exerciseId": "ex-006", "sets": 4, "reps": "8", "weight": "60", "restBetweenSets": 90, "setDetails": []}, {"exerciseId": "ex-010", "sets": 4, "reps": "8", "weight": "60", "restBetweenSets": 90, "setDetails": []}, {"exerciseId": "ex-012", "sets": 3, "reps": "10", "weight": "40", "restBetweenSets": 90, "setDetails": []}, {"exerciseId": "ex-042", "sets": 3, "reps": "12", "weight": "50", "restBetweenSets": 60, "setDetails": []}, {"exerciseId": "ex-043", "sets": 3, "reps": "15", "weight": "12", "restBetweenSets": 60, "setDetails": []}, {"exerciseId": "ex-055", "sets": 3, "reps": "20", "weight": "banda", "restBetweenSets": 45, "setDetails": []}], "created_at": "2026-01-21T10:00:00"},
  {"id": "rut-006", "name": "Tren Inferior — Fuerza", "description": "Sesión de piernas con foco en fuerza y cadena posterior.", "type": "classic", "difficulty": "intermediate", "tags": ["Tren Inferior", "Fuerza"], "circuit": None, "coach_id": "system", "exercises": [{"exerciseId": "ex-001", "sets": 4, "reps": "6", "weight": "75", "restBetweenSets": 180, "setDetails": []}, {"exerciseId": "ex-002", "sets": 4, "reps": "6", "weight": "90", "restBetweenSets": 180, "setDetails": []}, {"exerciseId": "ex-004", "sets": 3, "reps": "12", "weight": "60", "restBetweenSets": 90, "setDetails": []}, {"exerciseId": "ex-030", "sets": 3, "reps": "10", "weight": "30", "restBetweenSets": 90, "setDetails": []}, {"exerciseId": "ex-035", "sets": 3, "reps": "15", "weight": "BW", "restBetweenSets": 60, "setDetails": []}], "created_at": "2026-01-22T10:00:00"},
  {"id": "rut-007", "name": "Full Body B — Intermedio", "description": "Segundo día de cuerpo completo. Variantes distintas al Full Body A.", "type": "classic", "difficulty": "intermediate", "tags": ["Full Body", "Fuerza"], "circuit": None, "coach_id": "system", "exercises": [{"exerciseId": "ex-038", "sets": 4, "reps": "5", "weight": "90", "restBetweenSets": 180, "setDetails": []}, {"exerciseId": "ex-007", "sets": 3, "reps": "10", "weight": "20", "restBetweenSets": 90, "setDetails": []}, {"exerciseId": "ex-047", "sets": 3, "reps": "10", "weight": "25", "restBetweenSets": 90, "setDetails": []}, {"exerciseId": "ex-003", "sets": 3, "reps": "8", "weight": "22", "restBetweenSets": 90, "setDetails": []}, {"exerciseId": "ex-058", "sets": 3, "reps": "12", "weight": "20", "restBetweenSets": 60, "setDetails": []}, {"exerciseId": "ex-043", "sets": 3, "reps": "15", "weight": "10", "restBetweenSets": 45, "setDetails": []}], "created_at": "2026-01-23T10:00:00"},
  {"id": "rut-008", "name": "Hipertrofia Pecho y Hombros", "description": "Día dedicado a pectoral y deltoides. Volumen alto con series múltiples.", "type": "classic", "difficulty": "intermediate", "tags": ["Hipertrofia", "Pecho", "Hombros"], "circuit": None, "coach_id": "system", "exercises": [{"exerciseId": "ex-006", "sets": 4, "reps": "10", "weight": "70", "restBetweenSets": 90, "setDetails": []}, {"exerciseId": "ex-007", "sets": 3, "reps": "12", "weight": "22", "restBetweenSets": 75, "setDetails": []}, {"exerciseId": "ex-054", "sets": 3, "reps": "12", "weight": "20", "restBetweenSets": 75, "setDetails": []}, {"exerciseId": "ex-012", "sets": 4, "reps": "10", "weight": "40", "restBetweenSets": 90, "setDetails": []}, {"exerciseId": "ex-013", "sets": 3, "reps": "15", "weight": "8", "restBetweenSets": 60, "setDetails": []}, {"exerciseId": "ex-045", "sets": 3, "reps": "15", "weight": "6", "restBetweenSets": 60, "setDetails": []}], "created_at": "2026-01-24T10:00:00"},
  {"id": "rut-009", "name": "Hipertrofia Espalda y Bíceps", "description": "Tracción vertical y horizontal con aislamiento de bíceps.", "type": "classic", "difficulty": "intermediate", "tags": ["Hipertrofia", "Espalda", "Bíceps"], "circuit": None, "coach_id": "system", "exercises": [{"exerciseId": "ex-009", "sets": 4, "reps": "8", "weight": "BW", "restBetweenSets": 120, "setDetails": []}, {"exerciseId": "ex-011", "sets": 3, "reps": "12", "weight": "50", "restBetweenSets": 75, "setDetails": []}, {"exerciseId": "ex-010", "sets": 4, "reps": "10", "weight": "55", "restBetweenSets": 90, "setDetails": []}, {"exerciseId": "ex-049", "sets": 3, "reps": "12", "weight": "20", "restBetweenSets": 75, "setDetails": []}, {"exerciseId": "ex-014", "sets": 3, "reps": "12", "weight": "12", "restBetweenSets": 60, "setDetails": []}, {"exerciseId": "ex-046", "sets": 3, "reps": "15", "weight": "10", "restBetweenSets": 60, "setDetails": []}], "created_at": "2026-01-25T10:00:00"},
  {"id": "rut-010", "name": "Kettlebell Total Body", "description": "Sesión completa con pesa rusa. Potencia, fuerza y cardio integrados.", "type": "classic", "difficulty": "intermediate", "tags": ["Kettlebell", "Full Body", "Potencia"], "circuit": None, "coach_id": "system", "exercises": [{"exerciseId": "ex-064", "sets": 4, "reps": "15", "weight": "16", "restBetweenSets": 90, "setDetails": []}, {"exerciseId": "ex-065", "sets": 3, "reps": "10", "weight": "16", "restBetweenSets": 75, "setDetails": []}, {"exerciseId": "ex-067", "sets": 3, "reps": "8", "weight": "12", "restBetweenSets": 90, "setDetails": []}, {"exerciseId": "ex-069", "sets": 3, "reps": "40m", "weight": "24", "restBetweenSets": 90, "setDetails": []}, {"exerciseId": "ex-063", "sets": 2, "reps": "3c/lado", "weight": "8", "restBetweenSets": 120, "setDetails": []}], "created_at": "2026-01-26T10:00:00"},
  {"id": "rut-011", "name": "HIIT Metabólico 20-10", "description": "Circuito Tabata clásico. 20 segundos trabajo / 10 descanso. Alta intensidad.", "type": "circuit", "difficulty": "advanced", "tags": ["HIIT", "Tabata", "Metabólico"], "coach_id": "system", "circuit": {"rounds": 8, "work": 20, "rest_ex": 10, "rest_round": 60, "prep": 10}, "exercises": [{"exerciseId": "ex-076", "weight": "", "notes": "Máxima explosión"}, {"exerciseId": "ex-093", "weight": "", "notes": "Ritmo rápido"}, {"exerciseId": "ex-071", "weight": "10", "notes": "Peso moderado"}, {"exerciseId": "ex-080", "weight": "", "notes": "Aterrizaje suave"}, {"exerciseId": "ex-021", "weight": "", "notes": "Completos y rápidos"}], "created_at": "2026-01-27T10:00:00"},
  {"id": "rut-012", "name": "Circuito Funcional 40-15", "description": "Circuito funcional de 5 estaciones. Trabajo cardio-metabólico con pesos.", "type": "circuit", "difficulty": "intermediate", "tags": ["Funcional", "Circuito", "Cardio"], "coach_id": "system", "circuit": {"rounds": 4, "work": 40, "rest_ex": 15, "rest_round": 90, "prep": 15}, "exercises": [{"exerciseId": "ex-064", "weight": "16", "notes": "Explosión de cadera"}, {"exerciseId": "ex-039", "weight": "10", "notes": "8 por lado"}, {"exerciseId": "ex-062", "weight": "15", "notes": "Pies fijos"}, {"exerciseId": "ex-031", "weight": "12", "notes": "10 por pierna"}, {"exerciseId": "ex-017", "weight": "", "notes": "Cuerpo rígido"}], "created_at": "2026-01-28T10:00:00"},
  {"id": "rut-013", "name": "Prevención Rodilla", "description": "Rutina de fortalecimiento para prevenir lesiones de rodilla. LCA, meniscos y estabilizadores.", "type": "classic", "difficulty": "beginner", "tags": ["Prevención", "Rodilla"], "circuit": None, "coach_id": "system", "exercises": [{"exerciseId": "ex-099", "sets": 3, "reps": "15", "weight": "banda", "restBetweenSets": 60, "setDetails": []}, {"exerciseId": "ex-034", "sets": 3, "reps": "15", "weight": "20", "restBetweenSets": 60, "setDetails": []}, {"exerciseId": "ex-036", "sets": 3, "reps": "20", "weight": "banda", "restBetweenSets": 60, "setDetails": []}, {"exerciseId": "ex-100", "sets": 3, "reps": "20", "weight": "banda", "restBetweenSets": 60, "setDetails": []}, {"exerciseId": "ex-102", "sets": 3, "reps": "10", "weight": "BW", "restBetweenSets": 75, "setDetails": []}, {"exerciseId": "ex-105", "sets": 3, "reps": "45s", "weight": "BW", "restBetweenSets": 45, "setDetails": []}], "created_at": "2026-01-29T10:00:00"},
  {"id": "rut-014", "name": "Prevención Hombro", "description": "Fortalecimiento del manguito rotador y estabilizadores escapulares. Previene lesiones en pádel.", "type": "classic", "difficulty": "beginner", "tags": ["Prevención", "Hombro", "Manguito"], "circuit": None, "coach_id": "system", "exercises": [{"exerciseId": "ex-044", "sets": 3, "reps": "20", "weight": "banda", "restBetweenSets": 60, "setDetails": []}, {"exerciseId": "ex-043", "sets": 3, "reps": "15", "weight": "8", "restBetweenSets": 60, "setDetails": []}, {"exerciseId": "ex-055", "sets": 3, "reps": "20", "weight": "banda", "restBetweenSets": 45, "setDetails": []}, {"exerciseId": "ex-101", "sets": 3, "reps": "12", "weight": "2", "restBetweenSets": 60, "setDetails": []}, {"exerciseId": "ex-050", "sets": 3, "reps": "15", "weight": "10", "restBetweenSets": 60, "setDetails": []}], "created_at": "2026-01-30T10:00:00"},
  {"id": "rut-015", "name": "Activación y Movilidad General", "description": "Rutina de calentamiento y movilidad completa. Ideal antes de cualquier entrenamiento.", "type": "classic", "difficulty": "beginner", "tags": ["Movilidad", "Calentamiento"], "circuit": None, "coach_id": "system", "exercises": [{"exerciseId": "ex-091", "sets": 1, "reps": "60s", "weight": "", "restBetweenSets": 15, "setDetails": []}, {"exerciseId": "ex-090", "sets": 2, "reps": "8", "weight": "", "restBetweenSets": 15, "setDetails": []}, {"exerciseId": "ex-089", "sets": 2, "reps": "10", "weight": "", "restBetweenSets": 15, "setDetails": []}, {"exerciseId": "ex-083", "sets": 2, "reps": "10c/lado", "weight": "", "restBetweenSets": 15, "setDetails": []}, {"exerciseId": "ex-085", "sets": 2, "reps": "8c/lado", "weight": "", "restBetweenSets": 15, "setDetails": []}, {"exerciseId": "ex-084", "sets": 2, "reps": "10c/lado", "weight": "", "restBetweenSets": 15, "setDetails": []}, {"exerciseId": "ex-025", "sets": 2, "reps": "5c/lado", "weight": "", "restBetweenSets": 15, "setDetails": []}], "created_at": "2026-01-31T10:00:00"},
  {"id": "rut-016", "name": "Potencia Explosiva — Pliométrico", "description": "Desarrollo de potencia con ejercicios pliométricos. Para deportistas intermedios y avanzados.", "type": "classic", "difficulty": "advanced", "tags": ["Potencia", "Pliométrico", "Explosivo"], "circuit": None, "coach_id": "system", "exercises": [{"exerciseId": "ex-076", "sets": 4, "reps": "5", "weight": "", "restBetweenSets": 120, "setDetails": []}, {"exerciseId": "ex-022", "sets": 4, "reps": "5", "weight": "", "restBetweenSets": 120, "setDetails": []}, {"exerciseId": "ex-027", "sets": 3, "reps": "8c/lado", "weight": "", "restBetweenSets": 90, "setDetails": []}, {"exerciseId": "ex-078", "sets": 3, "reps": "5", "weight": "", "restBetweenSets": 90, "setDetails": []}, {"exerciseId": "ex-081", "sets": 3, "reps": "8", "weight": "5", "restBetweenSets": 90, "setDetails": []}], "created_at": "2026-02-01T10:00:00"},
  {"id": "rut-017", "name": "Pádel — Fuerza y Transferencia", "description": "Entrenamiento híbrido completo para padelistas. Fuerza, potencia y transferencia al golpe.", "type": "hybrid_padel", "difficulty": "intermediate", "tags": ["Pádel", "Híbrida", "Fuerza"], "circuit": None, "coach_id": "system", "blocks": [{"name": "Bloque 1", "exercises": [{"exerciseId": "ex-001", "type": "fuerza"}, {"exerciseId": "ex-117", "type": "transferencia"}, {"exerciseId": "ex-106", "type": "tecnica"}], "sets": 3, "setDetails": [{"weight_fuerza": "60", "weight_transfer": "banda", "weight_tecnica": "pala", "rest": 90, "notes": "Foco en la mecánica rotacional"}, {"weight_fuerza": "65", "weight_transfer": "banda", "weight_tecnica": "pala", "rest": 90, "notes": ""}, {"weight_fuerza": "70", "weight_transfer": "banda", "weight_tecnica": "pala", "rest": 90, "notes": ""}]}, {"name": "Bloque 2", "exercises": [{"exerciseId": "ex-027", "type": "fuerza"}, {"exerciseId": "ex-119", "type": "transferencia"}, {"exerciseId": "ex-128", "type": "tecnica"}], "sets": 3, "setDetails": [{"weight_fuerza": "BW", "weight_transfer": "BW", "weight_tecnica": "BW", "rest": 90, "notes": "Split step sincronizado"}, {"weight_fuerza": "BW", "weight_transfer": "BW", "weight_tecnica": "BW", "rest": 90, "notes": ""}, {"weight_fuerza": "BW", "weight_transfer": "BW", "weight_tecnica": "BW", "rest": 90, "notes": ""}]}], "exercises": [], "created_at": "2026-02-02T10:00:00"},
  {"id": "rut-018", "name": "Pádel — Agilidad y Reacción", "description": "Sesión específica de agilidad para padelistas. Desplazamientos, cambios de dirección y reflejos.", "type": "circuit", "difficulty": "intermediate", "tags": ["Pádel", "Agilidad", "Velocidad"], "coach_id": "system", "circuit": {"rounds": 4, "work": 30, "rest_ex": 20, "rest_round": 90, "prep": 10}, "exercises": [{"exerciseId": "ex-121", "weight": "", "notes": "Patrón lateral doble"}, {"exerciseId": "ex-028", "weight": "", "notes": "10 split steps"}, {"exerciseId": "ex-123", "weight": "", "notes": "T-Drill completo"}, {"exerciseId": "ex-126", "weight": "", "notes": "Señal coach"}, {"exerciseId": "ex-125", "weight": "", "notes": "Voleos a la pared"}], "created_at": "2026-02-03T10:00:00"},
  {"id": "rut-019", "name": "Pádel — Golpes Técnicos", "description": "Trabajo técnico de golpes. Ideal para principiantes e intermedios que quieren mejorar la mecánica.", "type": "circuit", "difficulty": "beginner", "tags": ["Pádel", "Técnico", "Golpes"], "coach_id": "system", "circuit": {"rounds": 3, "work": 45, "rest_ex": 15, "rest_round": 60, "prep": 10}, "exercises": [{"exerciseId": "ex-115", "weight": "", "notes": "Variedad de efectos"}, {"exerciseId": "ex-106", "weight": "", "notes": "Muñeca bloqueada"}, {"exerciseId": "ex-107", "weight": "", "notes": "Hombro no dominante adelante"}, {"exerciseId": "ex-113", "weight": "", "notes": "Preparación temprana"}, {"exerciseId": "ex-127", "weight": "", "notes": "Bounce continuo"}], "created_at": "2026-02-04T10:00:00"},
  {"id": "rut-020", "name": "Pádel — Híbrida Avanzada Competitiva", "description": "Entrenamiento integrado para padelistas competitivos. Fuerza máxima + transferencia + golpes específicos.", "type": "hybrid_padel", "difficulty": "advanced", "tags": ["Pádel", "Competitivo", "Híbrida", "Avanzado"], "circuit": None, "coach_id": "system", "blocks": [{"name": "Bloque 1 — Potencia", "exercises": [{"exerciseId": "ex-038", "type": "fuerza"}, {"exerciseId": "ex-081", "type": "transferencia"}, {"exerciseId": "ex-111", "type": "tecnica"}], "sets": 3, "setDetails": [{"weight_fuerza": "90", "weight_transfer": "4", "weight_tecnica": "pala", "rest": 120, "notes": "Remate máximo"}, {"weight_fuerza": "95", "weight_transfer": "4", "weight_tecnica": "pala", "rest": 120, "notes": ""}, {"weight_fuerza": "100", "weight_transfer": "5", "weight_tecnica": "pala", "rest": 120, "notes": ""}]}, {"name": "Bloque 2 — Lateral", "exercises": [{"exerciseId": "ex-039", "type": "fuerza"}, {"exerciseId": "ex-082", "type": "transferencia"}, {"exerciseId": "ex-109", "type": "tecnica"}], "sets": 3, "setDetails": [{"weight_fuerza": "14", "weight_transfer": "BW", "weight_tecnica": "pala", "rest": 120, "notes": "Víbora con máxima pronación"}, {"weight_fuerza": "14", "weight_transfer": "BW", "weight_tecnica": "pala", "rest": 120, "notes": ""}, {"weight_fuerza": "16", "weight_transfer": "BW", "weight_tecnica": "pala", "rest": 120, "notes": ""}]}, {"name": "Bloque 3 — Reactivo", "exercises": [{"exerciseId": "ex-074", "type": "fuerza"}, {"exerciseId": "ex-126", "type": "transferencia"}, {"exerciseId": "ex-116", "type": "tecnica"}], "sets": 3, "setDetails": [{"weight_fuerza": "BW", "weight_transfer": "BW", "weight_tecnica": "pala", "rest": 90, "notes": "Dejada perfecta"}, {"weight_fuerza": "BW", "weight_transfer": "BW", "weight_tecnica": "pala", "rest": 90, "notes": ""}, {"weight_fuerza": "BW", "weight_transfer": "BW", "weight_tecnica": "pala", "rest": 90, "notes": ""}]}], "exercises": [], "created_at": "2026-02-05T10:00:00"},
  {"id": "rut-021", "name": "Running — Fuerza Específica", "description": "Fuerza para corredores. Prevención de lesiones y mejora de la economía de carrera.", "type": "classic", "difficulty": "intermediate", "tags": ["Running", "Fuerza", "Prevención"], "circuit": None, "coach_id": "system", "exercises": [{"exerciseId": "ex-040", "sets": 3, "reps": "6", "weight": "BW", "restBetweenSets": 120, "setDetails": []}, {"exerciseId": "ex-102", "sets": 3, "reps": "10c/lado", "weight": "BW", "restBetweenSets": 90, "setDetails": []}, {"exerciseId": "ex-103", "sets": 3, "reps": "8", "weight": "BW", "restBetweenSets": 90, "setDetails": []}, {"exerciseId": "ex-004", "sets": 3, "reps": "15", "weight": "30", "restBetweenSets": 60, "setDetails": []}, {"exerciseId": "ex-036", "sets": 3, "reps": "20", "weight": "banda", "restBetweenSets": 60, "setDetails": []}], "created_at": "2026-02-06T10:00:00"},
  {"id": "rut-022", "name": "Recuperación Activa", "description": "Sesión de recuperación con movilidad, foam roller y ejercicios de bajo impacto.", "type": "classic", "difficulty": "beginner", "tags": ["Recuperación", "Movilidad", "Activo"], "circuit": None, "coach_id": "system", "exercises": [{"exerciseId": "ex-091", "sets": 1, "reps": "90s", "weight": "", "restBetweenSets": 30, "setDetails": []}, {"exerciseId": "ex-092", "sets": 1, "reps": "90s", "weight": "", "restBetweenSets": 30, "setDetails": []}, {"exerciseId": "ex-086", "sets": 2, "reps": "60sc/lado", "weight": "", "restBetweenSets": 30, "setDetails": []}, {"exerciseId": "ex-087", "sets": 2, "reps": "10c/lado", "weight": "banda", "restBetweenSets": 30, "setDetails": []}, {"exerciseId": "ex-088", "sets": 2, "reps": "10", "weight": "", "restBetweenSets": 30, "setDetails": []}, {"exerciseId": "ex-059", "sets": 2, "reps": "10c/lado", "weight": "", "restBetweenSets": 30, "setDetails": []}], "created_at": "2026-02-07T10:00:00"},
  {"id": "rut-023", "name": "Principiante — Primera Semana", "description": "Rutina para personas que empiezan a entrenar. Movimientos básicos con peso corporal y carga leve.", "type": "classic", "difficulty": "beginner", "tags": ["Principiante", "Full Body", "Peso Corporal"], "circuit": None, "coach_id": "system", "exercises": [{"exerciseId": "ex-065", "sets": 3, "reps": "10", "weight": "12", "restBetweenSets": 90, "setDetails": []}, {"exerciseId": "ex-008", "sets": 3, "reps": "8", "weight": "BW", "restBetweenSets": 90, "setDetails": []}, {"exerciseId": "ex-059", "sets": 2, "reps": "10c/lado", "weight": "", "restBetweenSets": 60, "setDetails": []}, {"exerciseId": "ex-017", "sets": 3, "reps": "20s", "weight": "", "restBetweenSets": 60, "setDetails": []}, {"exerciseId": "ex-094", "sets": 2, "reps": "30", "weight": "", "restBetweenSets": 60, "setDetails": []}, {"exerciseId": "ex-090", "sets": 1, "reps": "5", "weight": "", "restBetweenSets": 30, "setDetails": []}], "created_at": "2026-02-08T10:00:00"},
]

def upsert_seed_item(table, item):
    """Insert seed item only if id does not exist yet."""
    existing = db_get_one(table, item["id"])
    if not existing:
        extra = {}
        if "coach_id" in item: extra["coach_id"] = item["coach_id"]
        db_upsert(table, item["id"], item, extra or None)
        return True
    return False

def init_data():
    init_db()
    created = []
    # Coaches and athletes: only if empty
    if not load("coaches"):
        save("coaches", COACHES_SEED); created.append(f"{len(COACHES_SEED)} coaches")
    if not load("athletes"):
        save("athletes", ATHLETES_SEED); created.append(f"{len(ATHLETES_SEED)} atletas")
    # Exercises and routines: always upsert seed items (safe — only adds missing ones)
    ex_added = sum(1 for e in EXERCISES_SEED if upsert_seed_item("exercises", e))
    rut_added = sum(1 for r in ROUTINES_SEED if upsert_seed_item("routines", r))
    if ex_added:  created.append(f"{ex_added} ejercicios nuevos")
    if rut_added: created.append(f"{rut_added} rutinas nuevas")
    if created: print("  ✓ Datos iniciales: " + ", ".join(created))
    else: print(f"  ✓ Seed OK ({len(EXERCISES_SEED)} ejercicios, {len(ROUTINES_SEED)} rutinas)")

# ── BMI ───────────────────────────────────────────────────────────────────────
def calc_bmi(w, h):
    try:
        w, h = float(w), float(h)
        if w <= 0 or h <= 0: return None
        v = w / ((h/100)**2)
        cat = "Bajo peso" if v<18.5 else "Normal" if v<25 else "Sobrepeso" if v<30 else "Obesidad"
        return {"value": round(v,1), "category": cat}
    except: return None

def enrich(a):
    a = dict(a)
    a["bmi"] = calc_bmi(a.get("weight"), a.get("height"))
    return a

# ═══════════════════════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/login", methods=["POST"])
def login():
    d     = request.json or {}
    email = d.get("email","").strip().lower()
    pwd   = d.get("password","")

    # Admin
    if email == ADMIN_EMAIL.lower() and pwd == ADMIN_PASSWORD:
        return jsonify({"ok":True,"role":"admin","name":"Admin","email":ADMIN_EMAIL,
                        "avatar":"https://i.pravatar.cc/150?u=admin"})

    # Coach
    coaches = db_get("coaches")
    coach   = next((c for c in coaches if c.get("email","").lower()==email and c.get("password","")==pwd), None)
    if coach:
        if coach.get("is_disabled") or coach.get("status")=="inactive":
            return jsonify({"ok":False,"error":"Tu cuenta de coach está desactivada. Contactá al administrador."}), 403
        return jsonify({"ok":True,"role":"coach","id":coach["id"],"name":coach["name"],
                        "email":coach["email"],"avatar":coach.get("avatar",""),
                        "specialty":coach.get("specialty","")})

    # Athlete
    athletes = db_get("athletes")
    athlete  = next((a for a in athletes if a.get("email","").lower()==email and a.get("password","")==pwd), None)
    if athlete:
        if athlete.get("is_disabled") or athlete.get("status")=="inactive":
            return jsonify({"ok":False,"error":"Tu cuenta está desactivada. Contactá a tu coach."}), 403
        return jsonify({"ok":True,"role":"athlete","id":athlete["id"],
                        "name":f"{athlete['first_name']} {athlete['last_name']}",
                        "first_name":athlete["first_name"],"last_name":athlete["last_name"],
                        "email":athlete["email"],"avatar":athlete.get("avatar",""),
                        "sport":athlete.get("sport",""),"training_id":athlete.get("training_id",""),
                        "coach_id":athlete.get("coach_id","")})

    return jsonify({"ok":False,"error":"Email o contraseña incorrectos."}), 401

# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN — COACHES
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/coaches", methods=["GET"])
def get_coaches():
    coaches = db_get("coaches")
    athletes = db_get("athletes")
    for c in coaches:
        c["athlete_count"] = sum(1 for a in athletes if a.get("coach_id")==c["id"])
    return jsonify(coaches)

@app.route("/api/coaches", methods=["POST"])
def create_coach():
    d = request.json
    email_new = d.get("email","").strip().lower()
    coaches = load("coaches")
    if any(c.get("email","").lower() == email_new for c in coaches):
        return jsonify({"error":"Email ya existe"}), 400
    new = {"id":uid("coach"),"name":d.get("name",""),"email":d.get("email",""),
           "password":d.get("password","coach123"),"specialty":d.get("specialty",""),
           "avatar":d.get("avatar") or f"https://i.pravatar.cc/150?u={uid('c')}",
           "is_disabled":False,"status":"active","created_at":datetime.now().isoformat()}
    db_upsert("coaches", new["id"], new); return jsonify(new),201

@app.route("/api/coaches/<cid>", methods=["PUT"])
def update_coach(cid):
    d = request.json
    c = db_get_one("coaches", cid)
    if not c: return jsonify({"error":"not found"}),404
    for k in ["name","email","password","specialty","avatar"]:
        if k in d: c[k]=d[k]
    db_upsert("coaches", cid, c)
    return jsonify(c)

@app.route("/api/coaches/<cid>/toggle", methods=["POST"])
def toggle_coach(cid):
    c = db_get_one("coaches", cid)
    if not c: return jsonify({"error":"not found"}),404
    c["is_disabled"] = not c.get("is_disabled",False)
    c["status"] = "inactive" if c["is_disabled"] else "active"
    db_upsert("coaches", cid, c)
    return jsonify({"ok":True,"is_disabled":c["is_disabled"]})

@app.route("/api/coaches/<cid>", methods=["DELETE"])
def delete_coach(cid):
    db_delete("coaches", cid); return jsonify({"ok":True})

# ═══════════════════════════════════════════════════════════════════════════════
# ATHLETES
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/athletes", methods=["GET"])
def get_athletes():
    coach_id = request.args.get("coach_id")
    if coach_id:
        athletes = db_query("SELECT data FROM athletes WHERE coach_id=%s", [coach_id])
    else:
        athletes = db_get("athletes")
    return jsonify([enrich(a) for a in athletes])

@app.route("/api/athletes/<aid>", methods=["GET"])
def get_athlete(aid):
    a = db_get_one("athletes", aid)
    return (jsonify(enrich(a)),200) if a else (jsonify({"error":"not found"}),404)

@app.route("/api/athletes", methods=["POST"])
def create_athlete():
    d = request.json
    new = {"id":uid("ath"),"first_name":d.get("first_name",""),"last_name":d.get("last_name",""),
           "email":d.get("email",""),"phone":d.get("phone",""),"sport":d.get("sport","Fitness General"),
           "level":d.get("level","beginner"),"age":d.get("age"),"height":d.get("height"),
           "weight":d.get("weight"),"goal":d.get("goal",""),"notes":d.get("notes",""),
           "hand":d.get("hand","derecho"),"padel_pos":d.get("padel_pos","drive"),
           "password":d.get("password","password123"),
           "avatar":d.get("avatar") or f"https://i.pravatar.cc/150?u={uid('u')}",
           "status":"active","is_disabled":False,"training_id":"",
           "coach_id":d.get("coach_id",""),"created_at":datetime.now().isoformat()}
    db_upsert("athletes", new["id"], new, {"coach_id": new["coach_id"]}); return jsonify(enrich(new)),201

@app.route("/api/athletes/<aid>", methods=["PUT"])
def update_athlete(aid):
    d = request.json
    a = db_get_one("athletes", aid)
    if not a: return jsonify({"error":"not found"}),404
    for k in ["first_name","last_name","email","phone","sport","level",
              "age","height","weight","goal","notes","hand","padel_pos",
              "password","training_id","avatar"]:
        if k in d: a[k]=d[k]
    db_upsert("athletes", aid, a, {"coach_id": a.get("coach_id","")})
    return jsonify(enrich(a))

@app.route("/api/athletes/<aid>", methods=["DELETE"])
def delete_athlete(aid):
    db_delete("athletes", aid); return jsonify({"ok":True})

@app.route("/api/athletes/<aid>/assign", methods=["POST"])
def assign_routine(aid):
    rid=request.json.get("routine_id","")
    a = db_get_one("athletes", aid)
    if not a: return jsonify({"error":"not found"}),404
    a["training_id"]=rid
    db_upsert("athletes", aid, a, {"coach_id": a.get("coach_id","")})
    return jsonify({"ok":True})

# ═══════════════════════════════════════════════════════════════════════════════
# SCHEDULES (rutinas programadas por fecha)
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/schedules", methods=["GET"])
def get_schedules():
    coach_id   = request.args.get("coach_id")
    athlete_id = request.args.get("athlete_id")
    date       = request.args.get("date")
    if athlete_id:
        items = db_query("SELECT data FROM schedules WHERE athlete_id=%s",[athlete_id])
    elif coach_id:
        items = db_query("SELECT data FROM schedules WHERE coach_id=%s",[coach_id])
    else:
        items = db_get("schedules")
    if date:
        items = [s for s in items if s.get("date")==date]
    return jsonify(items)

@app.route("/api/schedules", methods=["POST"])
def create_schedule():
    d=request.json
    athlete_id = d.get("athlete_id","")
    # Normalize coach self-assign: "self-{id}" → "coach-self-{id}"
    if athlete_id.startswith("self-") and not athlete_id.startswith("coach-self-"):
        athlete_id = "coach-self-" + athlete_id[5:]
    all_scheds = load("schedules")
    dup = next((s for s in all_scheds
                if s.get("athlete_id")==athlete_id
                and s.get("date")==d.get("date")
                and s.get("routine_id")==d.get("routine_id")), None)
    if dup: return jsonify(dup),200
    new={"id":uid("sch"),"athlete_id":athlete_id,
         "routine_id":d.get("routine_id",""),"coach_id":d.get("coach_id",""),
         "date":d.get("date",""),"completed":False,"seen":False,
         "created_at":datetime.now().isoformat()}
    db_upsert("schedules", new["id"], new, {"athlete_id":new["athlete_id"],"coach_id":new["coach_id"],"date_col":new["date"]}); return jsonify(new),201

@app.route("/api/schedules/<sid>", methods=["DELETE"])
def delete_schedule(sid):
    db_delete("schedules", sid); return jsonify({"ok":True})

@app.route("/api/schedules/<sid>/complete", methods=["PUT"])
def complete_schedule(sid):
    s=db_get_one("schedules", sid)
    if not s: return jsonify({"error":"not found"}),404
    s["completed"]=True
    db_upsert("schedules", sid, s, {"athlete_id":s["athlete_id"],"coach_id":s.get("coach_id",""),"date_col":s["date"]})
    return jsonify(s)

@app.route("/api/schedules/<sid>/seen", methods=["PUT"])
def seen_schedule(sid):
    s=db_get_one("schedules", sid)
    if not s: return jsonify({"error":"not found"}),404
    s["seen"]=True
    db_upsert("schedules", sid, s, {"athlete_id":s["athlete_id"],"coach_id":s.get("coach_id",""),"date_col":s["date"]})
    return jsonify({"ok":True})

@app.route("/api/schedules/today/<aid>", methods=["GET"])
def schedule_today(aid):
    from datetime import date as dt_date
    today=dt_date.today().isoformat()
    items=db_query("SELECT data FROM schedules WHERE athlete_id=%s AND date_col=%s",[aid,today])
    routines_map={r["id"]:r for r in db_get("routines")}
    for s in items: s["routine"]=routines_map.get(s.get("routine_id",""))
    return jsonify(items)

@app.route("/api/schedules/unseen/<aid>", methods=["GET"])
def unseen_schedules(aid):
    all_s=db_query("SELECT data FROM schedules WHERE athlete_id=%s",[aid])
    items=[s for s in all_s if not s.get("seen") and not s.get("completed")]
    return jsonify({"count":len(items),"items":items})

@app.route("/api/athletes/<aid>/toggle", methods=["POST"])
def toggle_athlete(aid):
    a = db_get_one("athletes", aid)
    if not a: return jsonify({"error":"not found"}),404
    a["is_disabled"] = not a.get("is_disabled",False)
    a["status"] = "inactive" if a["is_disabled"] else "active"
    db_upsert("athletes", aid, a, {"coach_id": a.get("coach_id","")})
    return jsonify({"ok":True,"is_disabled":a["is_disabled"]})

# ═══════════════════════════════════════════════════════════════════════════════
# EXERCISES (biblioteca global)
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/exercises", methods=["GET"])
def get_exercises(): return jsonify(db_get("exercises"))

@app.route("/api/exercises", methods=["POST"])
def create_exercise():
    d=request.json
    new={"id":uid("ex"),"name":d.get("name",""),"category":d.get("category","fuerza"),
         "muscle_group":d.get("muscle_group",""),"muscle_groups":d.get("muscle_groups",[]),
         "equipment":d.get("equipment","Peso Corporal"),"equipments":d.get("equipments",[]),
         "difficulty":d.get("difficulty","intermediate"),
         "description":d.get("description",""),"tips":d.get("tips",[]),"errors":d.get("errors",[]),
         "tags":d.get("tags",[]),"image":d.get("image",""),"video":d.get("video",""),
         "created_by":"coach"}
    db_upsert("exercises", new["id"], new); return jsonify(new),201

@app.route("/api/exercises/<eid>", methods=["DELETE"])
def delete_exercise(eid):
    db_delete("exercises", eid); return jsonify({"ok":True})

# ═══════════════════════════════════════════════════════════════════════════════
# ROUTINES (filtradas por coach)
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/routines", methods=["GET"])
def get_routines():
    coach_id=request.args.get("coach_id")
    if coach_id:
        all_routines=db_get("routines")
        # Show: own routines + system/seed routines (coach_id="system" or the seed coaches)
        routines=[r for r in all_routines
                  if r.get("coach_id")==coach_id
                  or r.get("coach_id") in ("system","","coach-001","coach-002")]
    else:
        routines=db_get("routines")
    return jsonify(routines)

@app.route("/api/routines/<rid>", methods=["GET"])
def get_routine(rid):
    r=db_get_one("routines", rid)
    return (jsonify(r),200) if r else (jsonify({"error":"not found"}),404)

@app.route("/api/routines", methods=["POST"])
def create_routine():
    d=request.json
    new={"id":uid("rut"),"name":d.get("name",""),"description":d.get("description",""),
         "type":d.get("type","classic"),"difficulty":d.get("difficulty","intermediate"),
         "exercises":d.get("exercises",[]),"blocks":d.get("blocks",[]),
         "tags":d.get("tags",[]),"circuit":d.get("circuit"),
         "coach_id":d.get("coach_id",""),"created_at":datetime.now().isoformat()}
    db_upsert("routines", new["id"], new, {"coach_id": new.get("coach_id","")}); return jsonify(new),201

@app.route("/api/routines/<rid>", methods=["PUT"])
def update_routine(rid):
    d=request.json
    r=db_get_one("routines", rid)
    if not r: return jsonify({"error":"not found"}),404
    for k in ["name","description","type","difficulty","exercises","blocks","tags","circuit"]:
        if k in d: r[k]=d[k]
    db_upsert("routines", rid, r, {"coach_id": r.get("coach_id","")})
    return jsonify(r)

@app.route("/api/routines/<rid>", methods=["DELETE"])
def delete_routine(rid):
    db_delete("routines", rid); return jsonify({"ok":True})

# ═══════════════════════════════════════════════════════════════════════════════
# SESSIONS
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/sessions", methods=["GET"])
def get_sessions():
    coach_id=request.args.get("coach_id")
    if coach_id:
        sessions=db_query(
            "SELECT s.data FROM sessions s JOIN athletes a ON s.athlete_id=a.id WHERE a.coach_id=%s",
            [coach_id])
    else:
        sessions=db_get("sessions")
    return jsonify(sessions)

@app.route("/api/sessions", methods=["POST"])
def create_session():
    d=request.json
    new={"id":uid("ses"),"athlete_id":d.get("athlete_id",""),"routine_id":d.get("routine_id",""),
         "date":datetime.now().isoformat(),"duration":d.get("duration",0),
         "difficulty":d.get("difficulty","normal"),"rating":d.get("rating",3),
         "comment":d.get("comment",""),"completed":True}
    db_upsert("sessions", new["id"], new, {"athlete_id": new["athlete_id"]}); return jsonify(new),201

@app.route("/api/sessions/athlete/<aid>", methods=["GET"])
def get_athlete_sessions(aid):
    routines={r["id"]:r["name"] for r in db_get("routines")}
    sessions=db_query("SELECT data FROM sessions WHERE athlete_id=%s", [aid])
    for s in sessions: s["routine_name"]=routines.get(s.get("routine_id",""),"Sesión")
    return jsonify(sessions)

@app.route("/api/sessions/<sid>/reply", methods=["PUT"])
def session_reply(sid):
    d=request.json
    s=db_get_one("sessions", sid)
    if not s: return jsonify({"error":"not found"}),404
    s["coach_reply"]=d.get("reply","")
    s["coach_reply_at"]=datetime.now().isoformat()
    s["coach_read"]=True
    db_upsert("sessions", sid, s, {"athlete_id": s["athlete_id"]})
    return jsonify(s)

@app.route("/api/sessions/<sid>/read", methods=["PUT"])
def session_mark_read(sid):
    s=db_get_one("sessions", sid)
    if not s: return jsonify({"error":"not found"}),404
    s["coach_read"]=True
    db_upsert("sessions", sid, s, {"athlete_id": s["athlete_id"]})
    return jsonify({"ok":True})

@app.route("/api/sessions/unread", methods=["GET"])
def sessions_unread():
    coach_id=request.args.get("coach_id","")
    athletes=db_query("SELECT data FROM athletes WHERE coach_id=%s",[coach_id])
    athlete_ids={a["id"] for a in athletes}
    sessions=db_query(
        "SELECT data FROM sessions WHERE athlete_id=ANY(%s)",
        [list(athlete_ids)] if athlete_ids else [[]])
    unread=[s for s in sessions if s.get("comment","") and not s.get("coach_read",False)]
    athletes_map={a["id"]:f"{a['first_name']} {a['last_name']}" for a in athletes}
    routines_map={r["id"]:r["name"] for r in db_get("routines")}
    for s in unread:
        s["athlete_name"]=athletes_map.get(s["athlete_id"],"?")
        s["routine_name"]=routines_map.get(s.get("routine_id",""),"Sesión")
    return jsonify({"unread":unread,"count":len(unread)})

# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN — STATS
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/admin/stats", methods=["GET"])
def admin_stats():
    coaches  = db_get("coaches")
    athletes = db_get("athletes")
    sessions = db_get("sessions")
    return jsonify({
        "coaches":  len(coaches),
        "athletes": len(athletes),
        "sessions": len(sessions),
        "active_coaches":  sum(1 for c in coaches  if not c.get("is_disabled")),
        "active_athletes": sum(1 for a in athletes if not a.get("is_disabled")),
    })

# ═══════════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════
# POSTS — Feed de contenido del coach
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/posts", methods=["GET"])
def get_posts():
    coach_id = request.args.get("coach_id")
    if coach_id:
        posts = db_query("SELECT data FROM posts WHERE coach_id=%s ORDER BY id DESC", [coach_id]) if USE_PG                 else sorted([p for p in load("posts") if p.get("coach_id")==coach_id],
                             key=lambda x: x.get("created_at",""), reverse=True)
    else:
        posts = load("posts")
    return jsonify(posts)

@app.route("/api/posts", methods=["POST"])
def create_post():
    d = request.json
    new = {
        "id":         uid("post"),
        "coach_id":   d.get("coach_id",""),
        "title":      d.get("title",""),
        "body":       d.get("body",""),
        "category":   d.get("category","general"),
        "image":      d.get("image",""),
        "created_at": datetime.now().isoformat(),
    }
    db_upsert("posts", new["id"], new, {"coach_id": new["coach_id"]})
    return jsonify(new), 201

@app.route("/api/posts/<pid>", methods=["PUT"])
def update_post(pid):
    d = request.json
    p = db_get_one("posts", pid)
    if not p: return jsonify({"error":"not found"}),404
    for k in ["title","body","category","image"]:
        if k in d: p[k]=d[k]
    db_upsert("posts", pid, p, {"coach_id": p.get("coach_id","")})
    return jsonify(p)

@app.route("/api/posts/<pid>", methods=["DELETE"])
def delete_post(pid):
    db_delete("posts", pid)
    return jsonify({"ok": True})

@app.route("/api/posts/feed/<aid>", methods=["GET"])
def posts_feed(aid):
    """Posts del coach del atleta, con flag seen por atleta."""
    athletes = load("athletes")
    athlete  = next((a for a in athletes if a["id"]==aid), None)
    if not athlete:
        return jsonify([])
    coach_id = athlete.get("coach_id","")
    if USE_PG:
        posts = db_query("SELECT data FROM posts WHERE coach_id=%s ORDER BY id DESC", [coach_id])
    else:
        posts = sorted([p for p in load("posts") if p.get("coach_id")==coach_id],
                       key=lambda x: x.get("created_at",""), reverse=True)
    # mark seen
    seen_key = f"seen_posts_{aid}"
    seen_raw = load("seen_posts") if not USE_PG else []
    seen_rec = next((x for x in seen_raw if x.get("athlete_id")==aid), None)
    seen_ids = set(seen_rec.get("ids",[]) if seen_rec else [])
    for p in posts:
        p["seen"] = p["id"] in seen_ids
    return jsonify(posts)

@app.route("/api/posts/seen/<aid>", methods=["POST"])
def mark_posts_seen(aid):
    """Marca todos los posts como vistos para este atleta."""
    d = request.json or {}
    post_ids = d.get("ids", [])
    if USE_PG:
        # Store in a simple key-value in sessions or as a special post
        return jsonify({"ok": True})
    else:
        seen_list = load("seen_posts")
        rec = next((x for x in seen_list if x.get("athlete_id")==aid), None)
        if rec:
            existing = set(rec.get("ids",[]))
            existing.update(post_ids)
            rec["ids"] = list(existing)
        else:
            seen_list.append({"id": uid("seen"), "athlete_id": aid, "ids": post_ids})
        save("seen_posts", seen_list)
    return jsonify({"ok": True})

# ═══════════════════════════════════════════════════════════════════════════════
# AI — GEMINI
# ═══════════════════════════════════════════════════════════════════════════════
def ask_gemini(prompt):
    if not GEMINI_KEY: return None
    try:
        r=requests.post(f"{GEMINI_URL}?key={GEMINI_KEY}",
            json={"contents":[{"parts":[{"text":prompt}]}]},timeout=20)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e: return None

def fallback_routine(exes, rtype):
    cats=[e.get("category","") for e in exes]
    msgs=[]
    empuje=any(c in ["hipertrofia","fuerza"] for c in cats)
    has_core="core" in cats
    has_mob="movilidad" in cats
    has_cardio="cardio" in cats
    if not has_core: msgs.append("• Agregá al menos un ejercicio de core para estabilizar la zona media.")
    if not has_mob: msgs.append("• Incluí movilidad al cierre para acelerar la recuperación.")
    if rtype=="circuit" and not has_cardio: msgs.append("• Para circuito, considerá un ejercicio cardiovascular como burpees o saltos.")
    if rtype in ["classic","1rm"] and not empuje: msgs.append("• Falta trabajo de empuje (press, fondos). Revisá el balance muscular.")
    if not msgs: msgs=["• Balance muscular correcto.","• Volumen adecuado para el tipo de rutina."]
    return "Análisis automático (Gemini no disponible):\n" + "\n".join(msgs)

def fallback_assign(athlete, routine, exes):
    nivel=athlete.get("level","beginner")
    rtype=routine.get("type","classic")
    sport=athlete.get("sport","")
    msgs=[]
    if nivel=="beginner" and rtype=="1rm":
        msgs.append("• Atención: rutina de 1RM para deportista principiante. Considerá empezar con clásica.")
    if "padel" in sport.lower() and rtype!="hybrid_padel":
        msgs.append("• El deportista practica pádel. Una rutina híbrida de pádel tendría mejor transferencia.")
    bmi=calc_bmi(athlete.get("weight"),athlete.get("height"))
    if bmi and bmi["value"]>30:
        msgs.append("• IMC elevado: priorizá ejercicios de bajo impacto articular al inicio.")
    if not msgs: msgs=["• La rutina es compatible con el perfil del deportista."]
    return "\n".join(msgs)

@app.route("/api/ai/analyze-routine", methods=["POST"])
def ai_analyze():
    data=request.json
    routine=data.get("routine",{})
    exes=data.get("exercises_info",[])
    rtype=routine.get("type","classic")
    tipo_label={"classic":"Fuerza clásica","1rm":"Fuerza máxima (1RM)","circuit":"Circuito/Tabata","hybrid_padel":"Híbrida pádel"}.get(rtype,rtype)
    ex_list="\n".join([f"- {e['name']} ({e.get('category','?')}): {e.get('sets','?')} series x {e.get('reps','?')} reps" for e in exes])
    cats=list(set(e.get("category","") for e in exes))
    prompt=(
        f"Sos un coach deportivo experto analizando una rutina.\n"
        f"Rutina: {routine.get('name','')} | Tipo: {tipo_label} | Dificultad: {routine.get('difficulty','')}\n"
        f"Categorías presentes: {', '.join(cats)}\n"
        f"Ejercicios:\n{ex_list}\n\n"
        f"Respondé en español con este formato exacto (sin saludos, sin intro):\n"
        f"BALANCE: [1 oración sobre balance muscular empuje/tracción/core]\n"
        f"VOLUMEN: [1 oración sobre si el volumen es adecuado para el tipo]\n"
        f"SUGERENCIA 1: [mejora concreta]\n"
        f"SUGERENCIA 2: [otra mejora concreta o 'Sin observaciones adicionales']\n"
    )
    text=ask_gemini(prompt)
    if not text:
        return jsonify({"resumen":fallback_routine(exes,rtype),"ai":False,"sections":None})
    sections={}
    for line in text.split("\n"):
        line=line.strip()
        if line.startswith("BALANCE:"):       sections["balance"]=line[8:].strip()
        elif line.startswith("VOLUMEN:"):      sections["volumen"]=line[8:].strip()
        elif line.startswith("SUGERENCIA 1:"): sections["sug1"]=line[13:].strip()
        elif line.startswith("SUGERENCIA 2:"): sections["sug2"]=line[13:].strip()
    return jsonify({"resumen":text,"ai":True,"sections":sections})

@app.route("/api/ai/assign-analysis", methods=["POST"])
def ai_assign():
    data=request.json
    athlete=data.get("athlete",{})
    routine=data.get("routine",{})
    exes=data.get("exercises_info",[])
    bmi=calc_bmi(athlete.get("weight"),athlete.get("height"))
    bmi_txt=f"IMC {bmi['value']} ({bmi['category']})" if bmi else "sin datos biométricos"
    nivel_label={"beginner":"Principiante","intermediate":"Intermedio","advanced":"Avanzado"}.get(athlete.get("level",""),"?")
    rtype=routine.get("type","classic")
    tipo_label={"classic":"Fuerza clásica","1rm":"Fuerza máxima 1RM","circuit":"Circuito/Tabata","hybrid_padel":"Híbrida pádel"}.get(rtype,rtype)
    ex_list="\n".join([f"- {e['name']} ({e.get('category','?')})" for e in exes[:8]])
    prompt=(
        f"Sos un coach experto. Analizá si esta rutina es apropiada para este deportista específico.\n\n"
        f"DEPORTISTA: {athlete.get('first_name','')} {athlete.get('last_name','')} | "
        f"{athlete.get('age','?')} años | {athlete.get('height','?')}cm / {athlete.get('weight','?')}kg | {bmi_txt}\n"
        f"Deporte: {athlete.get('sport','?')} | Nivel: {nivel_label} | Objetivo: {athlete.get('goal','no especificado')}\n\n"
        f"RUTINA: {routine.get('name','')} | Tipo: {tipo_label} | Dificultad: {routine.get('difficulty','')}\n"
        f"Ejercicios: {ex_list}\n\n"
        f"Respondé en español con este formato exacto (sin saludos):\n"
        f"COMPATIBILIDAD: [Excelente/Buena/Regular/Baja] — [razón en 1 oración]\n"
        f"PARA ESTE ATLETA: [observación específica del perfil del deportista y esta rutina]\n"
        f"AJUSTE SUGERIDO: [cambio concreto o 'Sin ajustes necesarios']\n"
        f"CARGA INICIAL: [recomendación de peso/intensidad para la primera semana]\n"
    )
    text=ask_gemini(prompt)
    if not text:
        return jsonify({"resumen":fallback_assign(athlete,routine,exes),"ai":False,"sections":None})
    sections={}
    for line in text.split("\n"):
        line=line.strip()
        if line.startswith("COMPATIBILIDAD:"):    sections["compat"]=line[15:].strip()
        elif line.startswith("PARA ESTE ATLETA:"): sections["atleta"]=line[17:].strip()
        elif line.startswith("AJUSTE SUGERIDO:"): sections["ajuste"]=line[16:].strip()
        elif line.startswith("CARGA INICIAL:"):    sections["carga"]=line[14:].strip()
    return jsonify({"resumen":text,"ai":True,"sections":sections,"bmi":bmi})

@app.route("/api/ai/athlete-analysis", methods=["POST"])
def ai_athlete():
    a=request.json.get("athlete",{})
    bmi=calc_bmi(a.get("weight"),a.get("height"))
    bmi_txt=f"IMC {bmi['value']} ({bmi['category']})" if bmi else "sin datos biométricos"
    tips_fb=[]
    if bmi:
        v=bmi["value"]
        if v<18.5:   tips_fb=["Priorizar ganancia muscular magra","Aumentar ingesta calórica con proteínas","Evitar cardio excesivo inicial"]
        elif v<25:   tips_fb=["Composición óptima para el deporte","Periodizar fuerza e hipertrofia","Monitorear rendimiento, no peso"]
        elif v<30:   tips_fb=["Combinar cardio 3x/sem con fuerza 2x/sem","Déficit calórico de 300-400 kcal/día","Priorizar ejercicios compuestos"]
        else:        tips_fb=["Empezar con bajo impacto","Consultar médico antes de alta intensidad","Foco en movilidad y acondicionamiento"]
    text=ask_gemini(
        f"Analizá este perfil deportivo. {a.get('first_name','')} {a.get('last_name','')} | "
        f"{a.get('age','?')} años | {a.get('height','?')}cm {a.get('weight','?')}kg | {bmi_txt}\n"
        f"Deporte: {a.get('sport','?')} | Nivel: {a.get('level','?')} | Objetivo: {a.get('goal','')}\n"
        f"Respondé: línea 1 sobre IMC deportivo, luego 3 recomendaciones con '•'. Sin intro."
    )
    if not text:
        return jsonify({"tips":tips_fb,"bmi":bmi,"ai":False,"analysis":f"Perfil: {bmi_txt}."})
    lines=[l.strip() for l in text.split("\n") if l.strip()]
    intro=lines[0] if lines else ""
    bullets=[l.lstrip("•-– ") for l in lines[1:] if l.startswith(("•","-","–"))]
    return jsonify({"analysis":intro,"tips":bullets or tips_fb,"bmi":bmi,"ai":True})

# ═══════════════════════════════════════════════════════════════════════════════
# Run init_data at module load — works with both gunicorn (Railway) and direct run
try:
    init_data()
except Exception as _e:
    print(f"  ⚠ init_data error: {_e}")

if __name__ == "__main__":
    pass  # init_data already ran above
    print("\n" + "="*58)
    print("  🏋️  CoachApp v4")
    print("  ➜   http://localhost:5000")
    print(f"  📁  Datos: {DATA_DIR.resolve()}")
    print(f"  👑  Admin:  {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
    print(f"  🏋  Coach:  coach@coachapp.com / coach123")
    print(f"  🏃  Atleta: juan@email.com / juan123")
    print(f"  🤖  Gemini: {'✓ ACTIVO' if GEMINI_KEY else '✗ fallback'}")
    print("="*58 + "\n")
    app.run(debug=True, port=5000)

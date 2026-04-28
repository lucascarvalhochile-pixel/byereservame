#!/usr/bin/env python3
# Inclui obs/anexos no import. v2.1
"""
BYERESERVAME — Sistema de consulta de vendas da LC Turismo
Substitui o Reservame com busca textual, filtros combinados, sem limite de 100 rows.
"""

import os
import json
import sqlite3
import hashlib
import csv
import io
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template_string, request, redirect, url_for,
    session, flash, jsonify, Response, g
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "byereservame-lc-2026-secret")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB max upload

DB_PATH = os.environ.get("DB_PATH", "byereservame.db")
ADMIN_SETUP_KEY = os.environ.get("ADMIN_SETUP_KEY", "lcturismo2026")

# ─── Destination Classifier ─────────────────────────────────────────────────

DESTINOS_VALIDOS = [
    "Santiago", "Atacama", "Uyuni", "Cusco", "Lima",
    "San Andres", "Cartagena", "Buenos Aires", "Bariloche",
    "Mendoza", "Ushuaia", "El Calafate", "Punta Cana", "Cancún"
]

DESTINO_TO_PAIS = {
    "Santiago": "Chile", "Atacama": "Chile", "Uyuni": "Chile",
    "Torres del Paine": "Chile",
    "Cusco": "Peru", "Lima": "Peru",
    "San Andres": "Colômbia", "Cartagena": "Colômbia",
    "Buenos Aires": "Argentina", "Bariloche": "Argentina",
    "Mendoza": "Argentina", "Ushuaia": "Argentina", "El Calafate": "Argentina",
    "Punta Cana": "República Dominicana",
    "Cancún": "México",
}

def get_pais(destino):
    """Retorna o país a partir do destino (cidade)."""
    return DESTINO_TO_PAIS.get(destino, "Chile")

def classify_destino(tour):
    """Classifica o destino (nível cidade) a partir do nome do tour.
    Regras validadas pelo Lucas em 27/04/2026 (55 correções aplicadas)."""
    t = (tour or "").strip()
    tl = t.lower()

    # ── Atacama / Uyuni (checar antes de Chile genérico) ──
    if "chi atma" in tl or "chiata" in tl or "atacama" in tl:
        return "Atacama"
    if "uyuni" in tl:
        return "Uyuni"

    # ── Peru (Cusco vs Lima) ──
    if any(kw in tl for kw in ("cusco", "koricancha", "machu", "humantay", "sacred",
            "valle sagrado", "moray", "salineras", "rainbow", "7 cores",
            "montanha 7", "ausangate", "boleto tur", "titicaca")):
        return "Cusco"
    if t.startswith(("Y Per", "Ww Pr", "Yz Per")):
        if any(kw in tl for kw in ("lima", "barranco", "miraflores", "ballesta", "huacachina", "pachacamac")):
            return "Lima"
        return "Cusco"

    # ── Colômbia (San Andrés vs Cartagena) ──
    if t.startswith(("Y Sai", "Yz Sai")):
        return "San Andres"
    if t.startswith(("Yz Ctg", "Y Col Ctg")):
        return "Cartagena"
    if t.startswith("Y Col"):
        if any(kw in tl for kw in ("sai", "san andre")):
            return "San Andres"
        return "Cartagena"

    # ── Argentina (por cidade) — incluindo prefixo "Zz Arg" ──
    if t.startswith(("Z Arg", "Zz Arg")) or "buenos aires" in tl:
        if "brc" in tl or "bariloche" in tl:
            return "Bariloche"
        if "mendoza" in tl:
            return "Mendoza"
        if "ush" in tl or "ushuaia" in tl:
            return "Ushuaia"
        if "calafate" in tl:
            return "El Calafate"
        return "Buenos Aires"
    if "bariloche" in tl:
        return "Bariloche"
    if "mendoza" in tl:
        return "Mendoza"
    if "ushuaia" in tl:
        return "Ushuaia"
    if "calafate" in tl:
        return "El Calafate"

    # ── Rep. Dominicana ──
    if t.startswith("X Rd") or "punta cana" in tl or "saona" in tl or "bavaro" in tl:
        return "Punta Cana"

    # ── México ──
    if t.startswith("X Mex") or "cancun" in tl or "tulum" in tl or "chichen" in tl:
        return "Cancún"

    # ── Chile / Santiago (default para Zz/Zzz/Zerando) ──
    # Checar sub-destinos Chile antes do fallback Santiago
    if t.startswith(("Zz", "Zzz")):
        # Zz Chi Atma = Atacama
        if "chi atma" in tl or "atma" in tl:
            return "Atacama"
        # Zz Arg = Argentina
        if "arg" in tl:
            if "brc" in tl or "bariloche" in tl:
                return "Bariloche"
            if "ush" in tl or "ushuaia" in tl:
                return "Ushuaia"
            return "Buenos Aires"
        return "Santiago"
    if t.startswith("Zerando"):
        if "bariloche" in tl or "brc" in tl:
            return "Bariloche"
        return "Santiago"

    # Fallback: keywords Chile
    if any(kw in tl for kw in ("portillo", "farellones", "valle nevado", "vinicola",
            "concha", "undurraga", "haras", "isla negra", "safari", "cordilheira",
            "skiday", "panoram", "cajon", "transfer")):
        return "Santiago"

    return "Santiago"

# ─── Database ────────────────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            nome TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'viewer',
            paises_acesso TEXT NOT NULL DEFAULT 'ALL',
            vendedores_acesso TEXT NOT NULL DEFAULT 'ALL',
            pode_exportar INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS access_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            login_at TEXT DEFAULT CURRENT_TIMESTAMP,
            ip TEXT
        );
        CREATE TABLE IF NOT EXISTS vendas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ce_id TEXT NOT NULL,
            data TEXT NOT NULL,
            nome TEXT,
            tour TEXT,
            pax TEXT,
            endereco TEXT,
            depto TEXT,
            telefone TEXT,
            vendedor TEXT,
            valor TEXT,
            pendiente TEXT,
            ano INTEGER,
            mes INTEGER,
            destino TEXT DEFAULT '',
            pais TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS venda_obs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ce_id TEXT NOT NULL,
            obs TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS venda_anexos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ce_id TEXT NOT NULL,
            url TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_vendas_ce_id ON vendas(ce_id);
        CREATE INDEX IF NOT EXISTS idx_vendas_data ON vendas(data);
        CREATE INDEX IF NOT EXISTS idx_vendas_nome ON vendas(nome);
        CREATE INDEX IF NOT EXISTS idx_vendas_vendedor ON vendas(vendedor);
        CREATE INDEX IF NOT EXISTS idx_vendas_tour ON vendas(tour);
        CREATE INDEX IF NOT EXISTS idx_vendas_ano ON vendas(ano);
        CREATE INDEX IF NOT EXISTS idx_obs_ce ON venda_obs(ce_id);
        CREATE INDEX IF NOT EXISTS idx_anexos_ce ON venda_anexos(ce_id);
        CREATE INDEX IF NOT EXISTS idx_vendas_destino ON vendas(destino);
        CREATE INDEX IF NOT EXISTS idx_vendas_pais ON vendas(pais);
    """)
    # Migration: add destino column if missing (existing DBs)
    cols = [row[1] for row in db.execute("PRAGMA table_info(vendas)").fetchall()]
    if "destino" not in cols:
        db.execute("ALTER TABLE vendas ADD COLUMN destino TEXT DEFAULT ''")
        db.commit()
        print("Migration: added 'destino' column to vendas", flush=True)
    if "pais" not in cols:
        db.execute("ALTER TABLE vendas ADD COLUMN pais TEXT DEFAULT ''")
        db.commit()
        print("Migration: added 'pais' column to vendas", flush=True)
    # Migration: add paises_acesso and pode_exportar to users if missing
    user_cols = [row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()]
    if "paises_acesso" not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN paises_acesso TEXT NOT NULL DEFAULT 'ALL'")
        db.commit()
        print("Migration: added 'paises_acesso' column to users", flush=True)
    if "vendedores_acesso" not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN vendedores_acesso TEXT NOT NULL DEFAULT 'ALL'")
        db.commit()
        print("Migration: added 'vendedores_acesso' column to users", flush=True)
    if "pode_exportar" not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN pode_exportar INTEGER NOT NULL DEFAULT 0")
        db.commit()
        print("Migration: added 'pode_exportar' column to users", flush=True)
    # Backfill/reclassify ALL vendas (destino + pais) on every startup
    # This ensures any corrections to classify_destino are applied
    all_rows = db.execute("SELECT id, tour FROM vendas").fetchall()
    updated = 0
    for row in all_rows:
        dest = classify_destino(row[1])
        pais = get_pais(dest)
        db.execute("UPDATE vendas SET destino = ?, pais = ? WHERE id = ?", (dest, pais, row[0]))
        updated += 1
    if updated:
        db.commit()
        print(f"Backfill: classified {updated} vendas (destino + pais)", flush=True)
    # Create default users if no users exist
    cur = db.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] == 0:
        default_users = [
            ("admin", "admin123", "Administrador", "admin", "ALL", "ALL", 1),
            ("atendimento", "atend2026", "Atendimento", "viewer", "ALL", "ALL", 0),
            ("operacao", "oper2026", "Operação", "viewer", "ALL", "ALL", 1),
            ("yacana", "yacana2026", "Yacana Passeios", "viewer", "ALL", "felipegonzalezyacana,flaviasoaresyacana,jalvesyacanalc,mchavesposyacana,yfernandesyacanalc", 1),
        ]
        for u, pw, nome, role, paises, vendedores, csv_ok in default_users:
            pw_hash = hashlib.sha256(pw.encode()).hexdigest()
            db.execute(
                "INSERT INTO users (username, password_hash, nome, role, paises_acesso, vendedores_acesso, pode_exportar) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (u, pw_hash, nome, role, paises, vendedores, csv_ok)
            )
        db.commit()
        print("Created default users: admin, atendimento, operacao", flush=True)
    db.close()

# ─── Auth ────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            flash("Acesso restrito a administradores.", "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated

# ─── Routes: Auth ────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        pw_hash = hashlib.sha256(password.encode()).hexdigest()
        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE username = ? AND password_hash = ?",
            (username, pw_hash)
        ).fetchone()
        if user:
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["nome"] = user["nome"]
            session["role"] = user["role"]
            session["paises_acesso"] = user["paises_acesso"] if "paises_acesso" in user.keys() else "ALL"
            session["vendedores_acesso"] = user["vendedores_acesso"] if "vendedores_acesso" in user.keys() else "ALL"
            session["pode_exportar"] = user["pode_exportar"] if "pode_exportar" in user.keys() else 1
            # Log access
            db.execute(
                "INSERT INTO access_log (user_id, username, ip) VALUES (?, ?, ?)",
                (user["id"], user["username"], request.remote_addr)
            )
            db.commit()
            return redirect(url_for("index"))
        flash("Usuário ou senha incorretos.", "error")
    return render_template_string(LOGIN_HTML)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ─── Routes: Main ───────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    db = get_db()
    # Get filter params
    q = request.args.get("q", "").strip()
    data_de = request.args.get("data_de", "")
    data_ate = request.args.get("data_ate", "")
    vendedor = request.args.get("vendedor", "")
    tour_filter = request.args.get("tour", "")
    destino_filter = request.args.get("destino", "")
    pais_filter = request.args.get("pais", "")
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 50))

    # User's country access filter
    user_paises = session.get("paises_acesso", "ALL")
    allowed_paises = None
    if user_paises != "ALL":
        allowed_paises = [p.strip() for p in user_paises.split(",") if p.strip()]

    # User's vendor access filter
    user_vendedores = session.get("vendedores_acesso", "ALL")
    allowed_vendedores = None
    if user_vendedores != "ALL":
        allowed_vendedores = [v.strip() for v in user_vendedores.split(",") if v.strip()]

    # Build query
    conditions = []
    params = []

    # Restrict to allowed countries
    if allowed_paises:
        placeholders = ",".join("?" * len(allowed_paises))
        conditions.append(f"v.pais IN ({placeholders})")
        params.extend(allowed_paises)

    # Restrict to allowed vendors
    if allowed_vendedores:
        placeholders = ",".join("?" * len(allowed_vendedores))
        conditions.append(f"v.vendedor IN ({placeholders})")
        params.extend(allowed_vendedores)

    if q:
        conditions.append("(v.nome LIKE ? OR v.ce_id LIKE ? OR v.tour LIKE ? OR v.telefone LIKE ? OR v.endereco LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like, like, like])

    if data_de:
        conditions.append("v.data >= ?")
        params.append(data_de)
    if data_ate:
        conditions.append("v.data <= ?")
        params.append(data_ate)
    if vendedor:
        conditions.append("v.vendedor = ?")
        params.append(vendedor)
    if tour_filter:
        conditions.append("v.tour LIKE ?")
        params.append(f"%{tour_filter}%")
    if destino_filter:
        conditions.append("v.destino = ?")
        params.append(destino_filter)
    if pais_filter:
        conditions.append("v.pais = ?")
        params.append(pais_filter)

    where = " AND ".join(conditions) if conditions else "1=1"

    # Count
    count_sql = f"SELECT COUNT(*) FROM vendas v WHERE {where}"
    total = db.execute(count_sql, params).fetchone()[0]

    # Fetch page
    offset = (page - 1) * per_page
    data_sql = f"""
        SELECT v.*,
            (SELECT COUNT(*) FROM venda_obs o WHERE o.ce_id = v.ce_id) as obs_count,
            (SELECT COUNT(*) FROM venda_anexos a WHERE a.ce_id = v.ce_id) as anexo_count
        FROM vendas v
        WHERE {where}
        ORDER BY v.data DESC, v.ce_id DESC
        LIMIT ? OFFSET ?
    """
    rows = db.execute(data_sql, params + [per_page, offset]).fetchall()

    # Get vendedores and destinos for filter dropdowns
    vendedores = db.execute(
        "SELECT DISTINCT vendedor FROM vendas WHERE vendedor != '' ORDER BY vendedor"
    ).fetchall()
    destinos = db.execute(
        "SELECT DISTINCT destino FROM vendas WHERE destino != '' ORDER BY destino"
    ).fetchall()
    paises = db.execute(
        "SELECT DISTINCT pais FROM vendas WHERE pais != '' ORDER BY pais"
    ).fetchall()

    # Stats
    stats_sql = f"SELECT COUNT(*) as total, COUNT(DISTINCT vendedor) as vendedores, COUNT(DISTINCT data) as dias FROM vendas v WHERE {where}"
    stats = db.execute(stats_sql, params).fetchone()

    total_pages = (total + per_page - 1) // per_page

    return render_template_string(
        INDEX_HTML,
        rows=rows,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        q=q,
        data_de=data_de,
        data_ate=data_ate,
        vendedor=vendedor,
        tour_filter=tour_filter,
        destino_filter=destino_filter,
        pais_filter=pais_filter,
        vendedores=vendedores,
        destinos=destinos,
        paises=paises,
        stats=stats,
        user=session
    )

@app.route("/venda/<ce_id>")
@login_required
def venda_detail(ce_id):
    db = get_db()
    venda = db.execute("SELECT * FROM vendas WHERE ce_id = ?", (ce_id,)).fetchone()
    if not venda:
        flash("Venda não encontrada.", "error")
        return redirect(url_for("index"))

    obs = db.execute("SELECT obs FROM venda_obs WHERE ce_id = ? ORDER BY id", (ce_id,)).fetchall()
    anexos = db.execute("SELECT url FROM venda_anexos WHERE ce_id = ? ORDER BY id", (ce_id,)).fetchall()

    return render_template_string(DETAIL_HTML, venda=venda, obs=obs, anexos=anexos, user=session)

@app.route("/export")
@login_required
def export_csv():
    if not session.get("pode_exportar", 0):
        flash("Você não tem permissão para exportar CSV.", "error")
        return redirect(url_for("index"))
    db = get_db()
    q = request.args.get("q", "").strip()
    data_de = request.args.get("data_de", "")
    data_ate = request.args.get("data_ate", "")
    vendedor = request.args.get("vendedor", "")
    destino = request.args.get("destino", "")
    pais = request.args.get("pais", "")

    # Apply user country restriction
    user_paises = session.get("paises_acesso", "ALL")
    allowed_paises = None
    if user_paises != "ALL":
        allowed_paises = [p.strip() for p in user_paises.split(",") if p.strip()]

    # Apply user vendor restriction
    user_vendedores = session.get("vendedores_acesso", "ALL")
    allowed_vendedores = None
    if user_vendedores != "ALL":
        allowed_vendedores = [v.strip() for v in user_vendedores.split(",") if v.strip()]

    conditions = []
    params = []
    if allowed_paises:
        placeholders = ",".join("?" * len(allowed_paises))
        conditions.append(f"pais IN ({placeholders})")
        params.extend(allowed_paises)
    if allowed_vendedores:
        placeholders = ",".join("?" * len(allowed_vendedores))
        conditions.append(f"vendedor IN ({placeholders})")
        params.extend(allowed_vendedores)
    if q:
        conditions.append("(nome LIKE ? OR ce_id LIKE ? OR tour LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    if data_de:
        conditions.append("data >= ?")
        params.append(data_de)
    if data_ate:
        conditions.append("data <= ?")
        params.append(data_ate)
    if vendedor:
        conditions.append("vendedor = ?")
        params.append(vendedor)
    if destino:
        conditions.append("destino = ?")
        params.append(destino)
    if pais:
        conditions.append("pais = ?")
        params.append(pais)

    where = " AND ".join(conditions) if conditions else "1=1"
    rows = db.execute(f"SELECT * FROM vendas WHERE {where} ORDER BY data DESC", params).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Data", "Nome", "Tour", "País", "Destino", "PAX", "Endereço", "Depto", "Telefone", "Vendedor", "Valor", "Pendiente"])
    for r in rows:
        writer.writerow([r["ce_id"], r["data"], r["nome"], r["tour"], r.get("pais", ""), r.get("destino", ""),
                         r["pax"], r["endereco"], r["depto"], r["telefone"], r["vendedor"],
                         r["valor"], r["pendiente"]])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=byereservame_export_{datetime.now().strftime('%Y%m%d')}.csv"}
    )

# ─── Routes: Admin ──────────────────────────────────────────────────────────

@app.route("/admin/users")
@admin_required
def admin_users():
    db = get_db()
    users = db.execute("SELECT * FROM users ORDER BY username").fetchall()
    return render_template_string(ADMIN_USERS_HTML, users=users, user=session)

@app.route("/admin/users/add", methods=["POST"])
@admin_required
def admin_add_user():
    username = request.form.get("username", "").strip().lower()
    password = request.form.get("password", "")
    nome = request.form.get("nome", "").strip()
    role = request.form.get("role", "viewer")
    paises_acesso = request.form.get("paises_acesso", "ALL").strip() or "ALL"
    vendedores_acesso = request.form.get("vendedores_acesso", "ALL").strip() or "ALL"
    pode_exportar = 1 if request.form.get("pode_exportar") else 0

    if not username or not password or not nome:
        flash("Preencha todos os campos.", "error")
        return redirect(url_for("admin_users"))

    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    db = get_db()
    try:
        db.execute(
            "INSERT INTO users (username, password_hash, nome, role, paises_acesso, vendedores_acesso, pode_exportar) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (username, pw_hash, nome, role, paises_acesso, vendedores_acesso, pode_exportar)
        )
        db.commit()
        flash(f"Usuário {username} criado.", "success")
    except sqlite3.IntegrityError:
        flash(f"Usuário {username} já existe.", "error")
    return redirect(url_for("admin_users"))

@app.route("/admin/users/delete/<int:user_id>", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    db = get_db()
    db.execute("DELETE FROM users WHERE id = ? AND id != ?", (user_id, session["user_id"]))
    db.commit()
    flash("Usuário removido.", "success")
    return redirect(url_for("admin_users"))

@app.route("/admin/users/edit/<int:user_id>", methods=["POST"])
@admin_required
def admin_edit_user(user_id):
    db = get_db()
    paises_acesso = request.form.get("paises_acesso", "ALL").strip() or "ALL"
    vendedores_acesso = request.form.get("vendedores_acesso", "ALL").strip() or "ALL"
    pode_exportar = 1 if request.form.get("pode_exportar") else 0
    new_password = request.form.get("new_password", "").strip()
    if new_password:
        pw_hash = hashlib.sha256(new_password.encode()).hexdigest()
        db.execute("UPDATE users SET paises_acesso = ?, vendedores_acesso = ?, pode_exportar = ?, password_hash = ? WHERE id = ?",
                   (paises_acesso, vendedores_acesso, pode_exportar, pw_hash, user_id))
    else:
        db.execute("UPDATE users SET paises_acesso = ?, vendedores_acesso = ?, pode_exportar = ? WHERE id = ?",
                   (paises_acesso, vendedores_acesso, pode_exportar, user_id))
    db.commit()
    flash("Usuário atualizado.", "success")
    return redirect(url_for("admin_users"))

@app.route("/admin/access-log")
@admin_required
def admin_access_log():
    db = get_db()
    logs = db.execute("""
        SELECT * FROM access_log ORDER BY login_at DESC LIMIT 200
    """).fetchall()
    return render_template_string(ACCESS_LOG_HTML, logs=logs, user=session)

@app.route("/admin/stats")
@admin_required
def admin_stats():
    db = get_db()
    total_vendas = db.execute("SELECT COUNT(*) FROM vendas").fetchone()[0]
    total_obs = db.execute("SELECT COUNT(*) FROM venda_obs").fetchone()[0]
    total_anexos = db.execute("SELECT COUNT(*) FROM venda_anexos").fetchone()[0]
    by_month = db.execute("""
        SELECT ano, mes, COUNT(*) as qtd, COUNT(DISTINCT vendedor) as vendedores
        FROM vendas GROUP BY ano, mes ORDER BY ano, mes
    """).fetchall()
    by_vendedor = db.execute("""
        SELECT vendedor, COUNT(*) as qtd FROM vendas
        WHERE vendedor != '' GROUP BY vendedor ORDER BY qtd DESC LIMIT 20
    """).fetchall()
    return render_template_string(
        ADMIN_STATS_HTML,
        total_vendas=total_vendas,
        total_obs=total_obs,
        total_anexos=total_anexos,
        by_month=by_month,
        by_vendedor=by_vendedor,
        user=session
    )

# ─── Data Import ─────────────────────────────────────────────────────────────

@app.route("/admin/import", methods=["GET", "POST"])
@admin_required
def admin_import():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "import_2026":
            result = import_data_from_json()
            flash(result, "success")
        elif action == "upload_json":
            vendas_file = request.files.get("vendas_file")
            details_file = request.files.get("details_file")
            if not vendas_file:
                flash("Arquivo de vendas é obrigatório.", "error")
            else:
                try:
                    vendas = json.load(vendas_file)
                    details = json.load(details_file) if details_file and details_file.filename else []
                    result = import_data_from_upload(vendas, details)
                    flash(result, "success")
                except Exception as e:
                    flash(f"Erro ao processar JSON: {e}", "error")
        return redirect(url_for("admin_import"))
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM vendas").fetchone()[0]
    return render_template_string(ADMIN_IMPORT_HTML, total=total, user=session)

def import_data_from_upload(vendas, details):
    """Import data from uploaded JSON (via web form)."""
    details_map = {d["CE_ID"]: d for d in details} if details else {}

    db = sqlite3.connect(DB_PATH)
    count_before = db.execute("SELECT COUNT(*) FROM vendas").fetchone()[0]

    updated = 0
    for v in vendas:
        data = v.get("Data", "")
        ano = int(data[:4]) if len(data) >= 4 else 0
        mes = int(data[5:7]) if len(data) >= 7 else 0
        ce_id = v.get("ID", "")
        tour_name = v.get("Tour", "")
        destino_in = v.get("Destino", "")
        pais_in = v.get("Pais", "")
        destino = destino_in if destino_in else classify_destino(tour_name)
        pais = pais_in if pais_in else get_pais(destino)
        # Upsert: update if exists, insert if not
        existing = db.execute("SELECT 1 FROM vendas WHERE ce_id = ?", (ce_id,)).fetchone()
        if existing:
            db.execute("""
                UPDATE vendas SET data=?, nome=?, tour=?, pax=?, endereco=?, depto=?,
                    telefone=?, vendedor=?, valor=?, pendiente=?,
                    ano=?, mes=?, destino=?, pais=?
                WHERE ce_id=?
            """, (
                data, v.get("Nome", ""), tour_name, v.get("PAX", ""),
                v.get("Endereço", ""), v.get("Depto", ""),
                v.get("Telefone", ""), v.get("Vendedor", ""), v.get("Valor", ""),
                v.get("Pendiente", ""),
                ano, mes, destino, pais, ce_id
            ))
            updated += 1
        else:
            db.execute("""
                INSERT INTO vendas (ce_id, data, nome, tour, pax, endereco, depto, telefone, vendedor, valor, pendiente, ano, mes, destino, pais)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ce_id, data, v.get("Nome", ""), tour_name,
                v.get("PAX", ""), v.get("Endereço", ""), v.get("Depto", ""),
                v.get("Telefone", ""), v.get("Vendedor", ""), v.get("Valor", ""),
                v.get("Pendiente", ""), ano, mes, destino, pais
            ))

        # Process details (obs/anexos) for BOTH new and existing records
        detail = details_map.get(ce_id, {})
        if detail:
            db.execute("DELETE FROM venda_obs WHERE ce_id = ?", (ce_id,))
            db.execute("DELETE FROM venda_anexos WHERE ce_id = ?", (ce_id,))
            for obs in detail.get("observacoes", []):
                db.execute("INSERT INTO venda_obs (ce_id, obs) VALUES (?, ?)", (ce_id, obs))
            for url in detail.get("anexos", []):
                db.execute("INSERT INTO venda_anexos (ce_id, url) VALUES (?, ?)", (ce_id, url))

    db.commit()
    count_after = db.execute("SELECT COUNT(*) FROM vendas").fetchone()[0]
    db.close()
    added = count_after - count_before
    obs_count = sum(len(d.get('observacoes', [])) for d in details_map.values())
    anx_count = sum(len(d.get('anexos', [])) for d in details_map.values())
    return f"Importado: {added} novas, {updated} atualizadas (total: {count_after}), {obs_count} observações, {anx_count} anexos"

def import_data_from_json():
    """Import 2026 data from bundled JSON files."""
    main_path = os.path.join(os.path.dirname(__file__), "data", "vendas_2026.json")
    details_path = os.path.join(os.path.dirname(__file__), "data", "details_2026.json")

    if not os.path.exists(main_path):
        return f"Arquivo não encontrado: {main_path}"

    with open(main_path, encoding="utf-8") as f:
        vendas = json.load(f)

    details_map = {}
    if os.path.exists(details_path):
        with open(details_path, encoding="utf-8") as f:
            details = json.load(f)
        details_map = {d["CE_ID"]: d for d in details}

    db = sqlite3.connect(DB_PATH)
    db.execute("DELETE FROM vendas WHERE ano = 2026")
    db.execute("DELETE FROM venda_obs WHERE ce_id IN (SELECT ce_id FROM vendas WHERE ano = 2026)")
    # Clean all obs/anexos for re-import
    db.execute("DELETE FROM venda_obs")
    db.execute("DELETE FROM venda_anexos")

    for v in vendas:
        data = v.get("Data", "")
        ano = int(data[:4]) if len(data) >= 4 else 0
        mes = int(data[5:7]) if len(data) >= 7 else 0
        tour_name = v.get("Tour", "")
        destino = classify_destino(tour_name)
        pais = get_pais(destino)
        db.execute("""
            INSERT INTO vendas (ce_id, data, nome, tour, pax, endereco, depto, telefone, vendedor, valor, pendiente, ano, mes, destino, pais)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            v.get("ID", ""), data, v.get("Nome", ""), tour_name,
            v.get("PAX", ""), v.get("Endereço", ""), v.get("Depto", ""),
            v.get("Telefone", ""), v.get("Vendedor", ""), v.get("Valor", ""),
            v.get("Pendiente", ""), ano, mes, destino, pais
        ))

        # Import observations and attachments
        ce_id = v.get("ID", "")
        detail = details_map.get(ce_id, {})
        for obs in detail.get("observacoes", []):
            db.execute("INSERT INTO venda_obs (ce_id, obs) VALUES (?, ?)", (ce_id, obs))
        for url in detail.get("anexos", []):
            db.execute("INSERT INTO venda_anexos (ce_id, url) VALUES (?, ?)", (ce_id, url))

    db.commit()
    db.close()
    return f"Importado: {len(vendas)} vendas, {sum(len(d.get('observacoes',[])) for d in details_map.values())} observações, {sum(len(d.get('anexos',[])) for d in details_map.values())} anexos"

# ─── API ────────────────────────────────────────────────────────────────────

@app.route("/api/search")
@login_required
def api_search():
    db = get_db()
    q = request.args.get("q", "")
    limit = min(int(request.args.get("limit", 20)), 100)

    if not q:
        return jsonify([])

    like = f"%{q}%"
    rows = db.execute("""
        SELECT ce_id, data, nome, tour, vendedor, valor
        FROM vendas
        WHERE nome LIKE ? OR ce_id LIKE ? OR tour LIKE ? OR telefone LIKE ?
        ORDER BY data DESC LIMIT ?
    """, (like, like, like, like, limit)).fetchall()

    return jsonify([dict(r) for r in rows])

@app.route("/api/import", methods=["POST"])
def api_import():
    """API endpoint for bulk JSON import. Accepts multipart with vendas_file and optional details_file.
    Auth: requires admin_key query param matching ADMIN_API_KEY env var, or logged-in admin session."""
    api_key = os.environ.get("ADMIN_API_KEY", "byereservame2026")
    provided = request.args.get("key", "")
    is_admin_session = session.get("role") == "admin"
    if provided != api_key and not is_admin_session:
        return jsonify({"error": "Unauthorized"}), 401

    vendas_file = request.files.get("vendas_file")
    details_file = request.files.get("details_file")
    if not vendas_file:
        return jsonify({"error": "vendas_file is required"}), 400

    try:
        vendas = json.load(vendas_file)
        details = json.load(details_file) if details_file and details_file.filename else []
        result = import_data_from_upload(vendas, details)
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/enrich", methods=["POST"])
def api_enrich():
    """Enrich existing vendas with endereco, valor, depto, pendiente, obs, anexos.
    Accepts JSON body: list of objects with ID + fields to update.
    Only updates fields that are non-empty in the payload. Does NOT create new records.
    Obs and anexos are APPENDED (not replaced) to allow incremental enrichment.
    Auth: requires key query param."""
    api_key = os.environ.get("ADMIN_API_KEY", "byereservame2026")
    provided = request.args.get("key", "")
    if provided != api_key:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        data = request.get_json(force=True)
        if not isinstance(data, list):
            return jsonify({"error": "Expected JSON array"}), 400

        db = sqlite3.connect(DB_PATH)
        updated = 0
        not_found = 0
        obs_added = 0
        anexos_added = 0
        for item in data:
            ce_id = item.get("ID", "").strip()
            if not ce_id:
                continue
            existing = db.execute("SELECT 1 FROM vendas WHERE ce_id = ?", (ce_id,)).fetchone()
            if not existing:
                not_found += 1
                continue
            # Build dynamic SET clause — only update non-empty fields
            sets = []
            vals = []
            for json_key, db_col in [("Endereço", "endereco"), ("Depto", "depto"),
                                      ("Valor", "valor"), ("Pendiente", "pendiente")]:
                v = item.get(json_key, "")
                if v and str(v).strip():
                    sets.append(f"{db_col} = ?")
                    vals.append(str(v).strip())
            if sets:
                vals.append(ce_id)
                db.execute(f"UPDATE vendas SET {', '.join(sets)} WHERE ce_id = ?", vals)
                updated += 1

            # Append obs (don't delete existing)
            for obs_text in item.get("observacoes", []):
                if obs_text and str(obs_text).strip():
                    # Avoid duplicates
                    dup = db.execute("SELECT 1 FROM venda_obs WHERE ce_id = ? AND obs = ?",
                                    (ce_id, str(obs_text).strip())).fetchone()
                    if not dup:
                        db.execute("INSERT INTO venda_obs (ce_id, obs) VALUES (?, ?)",
                                   (ce_id, str(obs_text).strip()))
                        obs_added += 1

            # Append anexos (don't delete existing)
            for url in item.get("anexos", []):
                if url and str(url).strip():
                    dup = db.execute("SELECT 1 FROM venda_anexos WHERE ce_id = ? AND url = ?",
                                    (ce_id, str(url).strip())).fetchone()
                    if not dup:
                        db.execute("INSERT INTO venda_anexos (ce_id, url) VALUES (?, ?)",
                                   (ce_id, str(url).strip()))
                        anexos_added += 1

        db.commit()
        db.close()
        return jsonify({
            "ok": True,
            "updated": updated,
            "not_found": not_found,
            "obs_added": obs_added,
            "anexos_added": anexos_added,
            "total_sent": len(data)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── HTML Templates ─────────────────────────────────────────────────────────

BASE_CSS = """
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; background: #0f172a; color: #e2e8f0; }

    .navbar { background: #1e293b; padding: 12px 24px; display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid #334155; }
    .navbar .brand { font-size: 20px; font-weight: 700; color: #38bdf8; letter-spacing: 1px; }
    .navbar .brand span { color: #f43f5e; }
    .navbar nav a { color: #94a3b8; text-decoration: none; margin-left: 20px; font-size: 14px; transition: color 0.2s; }
    .navbar nav a:hover { color: #f8fafc; }
    .navbar .user-info { color: #64748b; font-size: 13px; }

    .container { max-width: 1400px; margin: 0 auto; padding: 24px; }

    .search-bar { background: #1e293b; border-radius: 12px; padding: 20px; margin-bottom: 20px; border: 1px solid #334155; }
    .search-bar .row { display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-end; }
    .search-bar label { display: block; color: #94a3b8; font-size: 12px; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.5px; }
    .search-bar input, .search-bar select {
        background: #0f172a; border: 1px solid #334155; color: #e2e8f0;
        padding: 10px 14px; border-radius: 8px; font-size: 14px; outline: none;
        transition: border-color 0.2s;
    }
    .search-bar input:focus, .search-bar select:focus { border-color: #38bdf8; }
    .search-bar input[type=text] { flex: 1; min-width: 200px; }
    .search-bar input[type=date] { width: 160px; }
    .search-bar select { min-width: 180px; }

    .btn {
        padding: 10px 20px; border-radius: 8px; font-size: 14px; font-weight: 600;
        cursor: pointer; border: none; transition: all 0.2s; text-decoration: none; display: inline-block;
    }
    .btn-primary { background: #2563eb; color: white; }
    .btn-primary:hover { background: #1d4ed8; }
    .btn-success { background: #059669; color: white; }
    .btn-success:hover { background: #047857; }
    .btn-danger { background: #dc2626; color: white; }
    .btn-danger:hover { background: #b91c1c; }
    .btn-ghost { background: transparent; color: #94a3b8; border: 1px solid #334155; }
    .btn-ghost:hover { background: #1e293b; color: #f8fafc; }
    .btn-sm { padding: 6px 12px; font-size: 12px; }

    .stats-bar { display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }
    .stat-card { background: #1e293b; border-radius: 10px; padding: 16px 20px; border: 1px solid #334155; min-width: 160px; }
    .stat-card .num { font-size: 24px; font-weight: 700; color: #38bdf8; }
    .stat-card .label { font-size: 12px; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px; }

    table { width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 12px; overflow: hidden; }
    thead th {
        background: #334155; color: #94a3b8; font-size: 11px; text-transform: uppercase;
        letter-spacing: 0.5px; padding: 12px 14px; text-align: left; position: sticky; top: 0;
    }
    tbody tr { border-bottom: 1px solid #1e293b; transition: background 0.15s; }
    tbody tr:hover { background: #334155; }
    tbody td { padding: 10px 14px; font-size: 13px; color: #cbd5e1; }
    tbody td a { color: #38bdf8; text-decoration: none; }
    tbody td a:hover { text-decoration: underline; }

    .badge {
        display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600;
    }
    .badge-obs { background: #1e3a5f; color: #38bdf8; }
    .badge-anexo { background: #3b1f2b; color: #f43f5e; }
    .badge-zero { background: #1e293b; color: #475569; }
    .badge-destino { background: #1a2e1a; color: #4ade80; font-size: 0.75rem; }
    .badge-pais { background: #2d1a3e; color: #c084fc; font-size: 0.75rem; }

    .pagination { display: flex; gap: 8px; justify-content: center; margin-top: 20px; align-items: center; }
    .pagination a, .pagination span {
        padding: 8px 14px; border-radius: 6px; font-size: 13px; text-decoration: none;
    }
    .pagination a { background: #1e293b; color: #94a3b8; border: 1px solid #334155; }
    .pagination a:hover { background: #334155; color: #f8fafc; }
    .pagination .active { background: #2563eb; color: white; border: 1px solid #2563eb; }
    .pagination .info { color: #64748b; font-size: 13px; }

    .flash { padding: 12px 20px; border-radius: 8px; margin-bottom: 16px; font-size: 14px; }
    .flash-error { background: #3b1f1f; color: #fca5a5; border: 1px solid #7f1d1d; }
    .flash-success { background: #1a3a2a; color: #6ee7b7; border: 1px solid #064e3b; }

    .detail-card { background: #1e293b; border-radius: 12px; padding: 24px; margin-bottom: 20px; border: 1px solid #334155; }
    .detail-card h3 { color: #38bdf8; margin-bottom: 16px; font-size: 16px; }
    .detail-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }
    .detail-field { }
    .detail-field .label { color: #64748b; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
    .detail-field .value { color: #e2e8f0; font-size: 15px; margin-top: 2px; }

    .obs-list { list-style: none; }
    .obs-list li { padding: 10px 14px; background: #0f172a; border-radius: 8px; margin-bottom: 8px; font-size: 13px; line-height: 1.5; border-left: 3px solid #38bdf8; }

    .anexo-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }
    .anexo-item {
        background: #0f172a; border-radius: 10px; font-size: 12px;
        color: #94a3b8; border: 1px solid #334155; overflow: hidden;
        transition: border-color 0.2s;
    }
    .anexo-item:hover { border-color: #38bdf8; }
    .anexo-item a { color: #38bdf8; text-decoration: none; display: block; }
    .anexo-thumb { width: 100%; aspect-ratio: 4/3; object-fit: cover; display: block; cursor: pointer; background: #0a0f1a; }
    .anexo-pdf-box { width: 100%; aspect-ratio: 4/3; display: flex; flex-direction: column; align-items: center; justify-content: center; background: #0a0f1a; cursor: pointer; }
    .anexo-pdf-icon { font-size: 40px; margin-bottom: 6px; }
    .anexo-pdf-label { color: #f43f5e; font-weight: 600; font-size: 13px; }
    .anexo-name { padding: 8px 10px; font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .anexo-modal { display: none; position: fixed; inset: 0; z-index: 9999; background: rgba(0,0,0,0.85); align-items: center; justify-content: center; cursor: zoom-out; }
    .anexo-modal.active { display: flex; }
    .anexo-modal img { max-width: 92vw; max-height: 90vh; border-radius: 8px; box-shadow: 0 0 40px rgba(0,0,0,0.5); }
    .anexo-modal-close { position: fixed; top: 16px; right: 24px; color: #fff; font-size: 32px; cursor: pointer; z-index: 10000; }

    .login-container {
        min-height: 100vh; display: flex; align-items: center; justify-content: center;
        background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
    }
    .login-box {
        background: #1e293b; border-radius: 16px; padding: 40px; width: 100%; max-width: 400px;
        border: 1px solid #334155; box-shadow: 0 25px 50px rgba(0,0,0,0.3);
    }
    .login-box h1 { text-align: center; margin-bottom: 8px; }
    .login-box .subtitle { text-align: center; color: #64748b; margin-bottom: 30px; font-size: 14px; }
    .login-box input {
        width: 100%; margin-bottom: 16px; padding: 12px 16px;
        background: #0f172a; border: 1px solid #334155; border-radius: 8px;
        color: #e2e8f0; font-size: 15px; outline: none;
    }
    .login-box input:focus { border-color: #38bdf8; }
    .login-box .btn { width: 100%; padding: 12px; font-size: 16px; }

    @media (max-width: 768px) {
        .container { padding: 12px; }
        .search-bar .row { flex-direction: column; }
        .search-bar input, .search-bar select { width: 100%; }
        .stats-bar { flex-direction: column; }
        table { font-size: 12px; }
        thead th, tbody td { padding: 8px 10px; }
    }
</style>
"""

LOGIN_HTML = """<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>BYERESERVAME — Login</title>""" + BASE_CSS + """</head><body>
<div class="login-container">
    <div class="login-box">
        <h1><span style="color:#38bdf8">BYE</span><span style="color:#f43f5e">RESERVAME</span></h1>
        <div class="subtitle">LC Turismo — Sistema de Vendas</div>
        {% for cat, msg in get_flashed_messages(with_categories=true) %}
        <div class="flash flash-{{ cat }}">{{ msg }}</div>
        {% endfor %}
        <form method="POST">
            <input type="text" name="username" placeholder="Usuário" required autofocus>
            <input type="password" name="password" placeholder="Senha" required>
            <button type="submit" class="btn btn-primary">Entrar</button>
        </form>
    </div>
</div>
</body></html>"""

INDEX_HTML = """<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>BYERESERVAME</title>""" + BASE_CSS + """</head><body>
<div class="navbar">
    <div class="brand">BYE<span>RESERVAME</span></div>
    <nav>
        <a href="{{ url_for('index') }}">Vendas</a>
        {% if user.role == 'admin' %}
        <a href="{{ url_for('admin_stats') }}">Estatísticas</a>
        <a href="{{ url_for('admin_users') }}">Usuários</a>
        <a href="{{ url_for('admin_access_log') }}">Acessos</a>
        <a href="{{ url_for('admin_import') }}">Importar</a>
        {% endif %}
    </nav>
    <div class="user-info">{{ user.nome }} · <a href="{{ url_for('logout') }}" style="color:#f43f5e">Sair</a></div>
</div>
<div class="container">
    {% for cat, msg in get_flashed_messages(with_categories=true) %}
    <div class="flash flash-{{ cat }}">{{ msg }}</div>
    {% endfor %}

    <div class="search-bar">
        <form method="GET" action="{{ url_for('index') }}">
            <div class="row">
                <div style="flex:2">
                    <label>Busca (nome, ID, tour, telefone)</label>
                    <input type="text" name="q" value="{{ q }}" placeholder="Buscar...">
                </div>
                <div>
                    <label>De</label>
                    <input type="date" name="data_de" value="{{ data_de }}">
                </div>
                <div>
                    <label>Até</label>
                    <input type="date" name="data_ate" value="{{ data_ate }}">
                </div>
                <div>
                    <label>Vendedor</label>
                    <select name="vendedor">
                        <option value="">Todos</option>
                        {% for v in vendedores %}
                        <option value="{{ v.vendedor }}" {% if v.vendedor == vendedor %}selected{% endif %}>{{ v.vendedor }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div>
                    <label>País</label>
                    <select name="pais">
                        <option value="">Todos</option>
                        {% for p in paises %}
                        <option value="{{ p.pais }}" {% if p.pais == pais_filter %}selected{% endif %}>{{ p.pais }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div>
                    <label>Destino</label>
                    <select name="destino">
                        <option value="">Todos</option>
                        {% for d in destinos %}
                        <option value="{{ d.destino }}" {% if d.destino == destino_filter %}selected{% endif %}>{{ d.destino }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div>
                    <label>Tour</label>
                    <input type="text" name="tour" value="{{ tour_filter }}" placeholder="Tour..." style="width:160px">
                </div>
                <div>
                    <button type="submit" class="btn btn-primary">Buscar</button>
                </div>
                <div>
                    <a href="{{ url_for('index') }}" class="btn btn-ghost">Limpar</a>
                </div>
                {% if user.pode_exportar %}
                <div>
                    <a href="{{ url_for('export_csv', q=q, data_de=data_de, data_ate=data_ate, vendedor=vendedor, destino=destino_filter, pais=pais_filter) }}" class="btn btn-success btn-sm">CSV</a>
                </div>
                {% endif %}
            </div>
        </form>
    </div>

    <div class="stats-bar">
        <div class="stat-card"><div class="num">{{ "{:,}".format(total).replace(",",".") }}</div><div class="label">Resultados</div></div>
        <div class="stat-card"><div class="num">{{ stats.vendedores }}</div><div class="label">Vendedores</div></div>
        <div class="stat-card"><div class="num">{{ stats.dias }}</div><div class="label">Dias</div></div>
    </div>

    <table>
        <thead>
            <tr>
                <th>ID</th><th>Data</th><th>Nome</th><th>Tour</th><th>País</th><th>Destino</th><th>PAX</th>
                <th>Vendedor</th><th>Valor</th><th>Pend.</th><th>Obs</th><th>Anexos</th>
            </tr>
        </thead>
        <tbody>
        {% for r in rows %}
            <tr>
                <td><a href="{{ url_for('venda_detail', ce_id=r.ce_id) }}">{{ r.ce_id }}</a></td>
                <td>{{ r.data }}</td>
                <td>{{ r.nome[:35] }}{% if r.nome|length > 35 %}...{% endif %}</td>
                <td>{{ r.tour[:30] }}{% if r.tour|length > 30 %}...{% endif %}</td>
                <td><span class="badge badge-pais">{{ r.pais }}</span></td>
                <td><span class="badge badge-destino">{{ r.destino }}</span></td>
                <td>{{ r.pax }}</td>
                <td>{{ r.vendedor }}</td>
                <td>{{ r.valor }}</td>
                <td>{{ r.pendiente }}</td>
                <td>{% if r.obs_count > 0 %}<span class="badge badge-obs">{{ r.obs_count }}</span>{% else %}<span class="badge badge-zero">0</span>{% endif %}</td>
                <td>{% if r.anexo_count > 0 %}<span class="badge badge-anexo">{{ r.anexo_count }}</span>{% else %}<span class="badge badge-zero">0</span>{% endif %}</td>
            </tr>
        {% endfor %}
        {% if not rows %}
            <tr><td colspan="11" style="text-align:center; padding:40px; color:#64748b;">Nenhum resultado encontrado.</td></tr>
        {% endif %}
        </tbody>
    </table>

    {% if total_pages > 1 %}
    <div class="pagination">
        {% if page > 1 %}
        <a href="?page={{ page-1 }}&q={{ q }}&data_de={{ data_de }}&data_ate={{ data_ate }}&vendedor={{ vendedor }}&destino={{ destino_filter }}&pais={{ pais_filter }}&tour={{ tour_filter }}">← Anterior</a>
        {% endif %}

        {% for p in range(1, total_pages+1) %}
            {% if p == page %}
                <span class="active">{{ p }}</span>
            {% elif p <= 3 or p >= total_pages-2 or (p >= page-2 and p <= page+2) %}
                <a href="?page={{ p }}&q={{ q }}&data_de={{ data_de }}&data_ate={{ data_ate }}&vendedor={{ vendedor }}&destino={{ destino_filter }}&pais={{ pais_filter }}&tour={{ tour_filter }}">{{ p }}</a>
            {% elif p == 4 or p == total_pages-3 %}
                <span class="info">...</span>
            {% endif %}
        {% endfor %}

        {% if page < total_pages %}
        <a href="?page={{ page+1 }}&q={{ q }}&data_de={{ data_de }}&data_ate={{ data_ate }}&vendedor={{ vendedor }}&destino={{ destino_filter }}&pais={{ pais_filter }}&tour={{ tour_filter }}">Próxima →</a>
        {% endif %}
        <span class="info">{{ total }} vendas · Página {{ page }}/{{ total_pages }}</span>
    </div>
    {% endif %}
</div>
</body></html>"""

DETAIL_HTML = """<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ venda.ce_id }} — BYERESERVAME</title>""" + BASE_CSS + """</head><body>
<div class="navbar">
    <div class="brand">BYE<span>RESERVAME</span></div>
    <nav>
        <a href="{{ url_for('index') }}">← Voltar</a>
    </nav>
    <div class="user-info">{{ user.nome }}</div>
</div>
<div class="container">
    <div class="detail-card">
        <h3>Venda {{ venda.ce_id }}</h3>
        <div class="detail-grid">
            <div class="detail-field"><div class="label">ID</div><div class="value">{{ venda.ce_id }}</div></div>
            <div class="detail-field"><div class="label">Data</div><div class="value">{{ venda.data }}</div></div>
            <div class="detail-field"><div class="label">Nome</div><div class="value">{{ venda.nome }}</div></div>
            <div class="detail-field"><div class="label">Tour</div><div class="value">{{ venda.tour }}</div></div>
            <div class="detail-field"><div class="label">País</div><div class="value"><span class="badge badge-pais">{{ venda.pais }}</span></div></div>
            <div class="detail-field"><div class="label">Destino</div><div class="value"><span class="badge badge-destino">{{ venda.destino }}</span></div></div>
            <div class="detail-field"><div class="label">PAX</div><div class="value">{{ venda.pax }}</div></div>
            <div class="detail-field"><div class="label">Endereço</div><div class="value">{{ venda.endereco }}</div></div>
            <div class="detail-field"><div class="label">Depto</div><div class="value">{{ venda.depto }}</div></div>
            <div class="detail-field"><div class="label">Telefone</div><div class="value">{{ venda.telefone }}</div></div>
            <div class="detail-field"><div class="label">Vendedor</div><div class="value">{{ venda.vendedor }}</div></div>
            <div class="detail-field"><div class="label">Valor</div><div class="value" style="color:#22c55e; font-weight:700">{{ venda.valor }}</div></div>
            <div class="detail-field"><div class="label">Pendiente</div><div class="value" style="color:#f59e0b">{{ venda.pendiente }}</div></div>
        </div>
    </div>

    <div class="detail-card">
        <h3>Observações ({{ obs|length }})</h3>
        {% if obs %}
        <ul class="obs-list">
            {% for o in obs %}
            <li>{{ o.obs }}</li>
            {% endfor %}
        </ul>
        {% else %}
        <p style="color:#64748b">Nenhuma observação registrada.</p>
        {% endif %}
    </div>

    <div class="detail-card">
        <h3>Anexos / Comprovantes ({{ anexos|length }})</h3>
        {% if anexos %}
        <div class="anexo-grid">
            {% for a in anexos %}
            {% set fname = a.url.split('/')[-1] %}
            {% set is_img = fname.lower().endswith(('.jpeg','.jpg','.png','.gif','.webp')) %}
            <div class="anexo-item">
                {% if is_img %}
                <img class="anexo-thumb" src="{{ a.url }}" alt="{{ fname }}" loading="lazy" onclick="openModal(this.src)">
                {% else %}
                <a href="{{ a.url }}" target="_blank">
                    <div class="anexo-pdf-box">
                        <div class="anexo-pdf-icon">&#128196;</div>
                        <div class="anexo-pdf-label">PDF</div>
                    </div>
                </a>
                {% endif %}
                <div class="anexo-name"><a href="{{ a.url }}" target="_blank">{{ fname }}</a></div>
            </div>
            {% endfor %}
        </div>
        <div class="anexo-modal" id="imgModal" onclick="closeModal()">
            <span class="anexo-modal-close">&times;</span>
            <img id="modalImg" src="">
        </div>
        <script>
        function openModal(src){var m=document.getElementById('imgModal');document.getElementById('modalImg').src=src;m.classList.add('active');}
        function closeModal(){document.getElementById('imgModal').classList.remove('active');}
        document.addEventListener('keydown',function(e){if(e.key==='Escape')closeModal();});
        </script>
        {% else %}
        <p style="color:#64748b">Nenhum anexo encontrado.</p>
        {% endif %}
    </div>
</div>
</body></html>"""

ADMIN_USERS_HTML = """<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Usuários — BYERESERVAME</title>""" + BASE_CSS + """
<style>
.inp{background:#0f172a;border:1px solid #334155;color:#e2e8f0;padding:6px 10px;border-radius:6px;font-size:13px}
.inp-sm{width:120px} .inp-md{width:180px}
.edit-row td{background:#1e293b !important}
label{display:block;color:#94a3b8;font-size:12px;margin-bottom:2px}
</style>
</head><body>
<div class="navbar">
    <div class="brand">BYE<span>RESERVAME</span></div>
    <nav>
        <a href="{{ url_for('index') }}">Vendas</a>
        <a href="{{ url_for('admin_stats') }}">Estatísticas</a>
        <a href="{{ url_for('admin_users') }}" style="color:#f8fafc">Usuários</a>
        <a href="{{ url_for('admin_access_log') }}">Acessos</a>
        <a href="{{ url_for('admin_import') }}">Importar</a>
    </nav>
    <div class="user-info">{{ user.nome }} · <a href="{{ url_for('logout') }}" style="color:#f43f5e">Sair</a></div>
</div>
<div class="container">
    {% for cat, msg in get_flashed_messages(with_categories=true) %}
    <div class="flash flash-{{ cat }}">{{ msg }}</div>
    {% endfor %}

    <div class="detail-card">
        <h3>Adicionar Usuário</h3>
        <form method="POST" action="{{ url_for('admin_add_user') }}" style="display:flex; gap:12px; flex-wrap:wrap; align-items:flex-end;">
            <div><label>Usuário</label><input type="text" name="username" required class="inp inp-sm"></div>
            <div><label>Senha</label><input type="text" name="password" required class="inp inp-sm"></div>
            <div><label>Nome</label><input type="text" name="nome" required class="inp inp-md"></div>
            <div><label>Papel</label>
                <select name="role" class="inp">
                    <option value="viewer">Visualizador</option>
                    <option value="admin">Administrador</option>
                </select>
            </div>
            <div><label>Países (ou ALL)</label><input type="text" name="paises_acesso" value="ALL" class="inp inp-md" placeholder="Chile,Argentina ou ALL"></div>
            <div><label>Vendedores (ou ALL)</label><input type="text" name="vendedores_acesso" value="ALL" class="inp inp-md" placeholder="usuario1,usuario2 ou ALL"></div>
            <div style="display:flex;align-items:center;gap:6px;padding-bottom:4px">
                <input type="checkbox" name="pode_exportar" id="add_csv" value="1">
                <label for="add_csv" style="margin:0;color:#e2e8f0">Exportar CSV</label>
            </div>
            <button type="submit" class="btn btn-primary btn-sm">Criar</button>
        </form>
    </div>

    <table>
        <thead><tr><th>ID</th><th>Usuário</th><th>Nome</th><th>Papel</th><th>Países</th><th>Vendedores</th><th>CSV</th><th>Criado</th><th>Ações</th></tr></thead>
        <tbody>
        {% for u in users %}
        <tr>
            <td>{{ u.id }}</td><td>{{ u.username }}</td><td>{{ u.nome }}</td>
            <td><span class="badge {% if u.role == 'admin' %}badge-anexo{% else %}badge-obs{% endif %}">{{ u.role }}</span></td>
            <td>{{ u.paises_acesso if u.paises_acesso else 'ALL' }}</td>
            <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="{{ u.vendedores_acesso if u.vendedores_acesso else 'ALL' }}">{{ u.vendedores_acesso if u.vendedores_acesso else 'ALL' }}</td>
            <td>{% if u.pode_exportar %}<span style="color:#22c55e">Sim</span>{% else %}<span style="color:#f43f5e">Não</span>{% endif %}</td>
            <td>{{ u.created_at[:10] if u.created_at else '' }}</td>
            <td style="white-space:nowrap">
                {% if u.id != user.user_id %}
                <button class="btn btn-sm" onclick="document.getElementById('edit-{{u.id}}').style.display=document.getElementById('edit-{{u.id}}').style.display==='none'?'table-row':'none'" style="background:#334155;color:#e2e8f0;margin-right:4px">Editar</button>
                <form method="POST" action="{{ url_for('admin_delete_user', user_id=u.id) }}" style="display:inline" onsubmit="return confirm('Remover {{ u.username }}?')">
                    <button type="submit" class="btn btn-danger btn-sm">Remover</button>
                </form>
                {% endif %}
            </td>
        </tr>
        {% if u.id != user.user_id %}
        <tr id="edit-{{u.id}}" class="edit-row" style="display:none">
            <td colspan="9">
                <form method="POST" action="{{ url_for('admin_edit_user', user_id=u.id) }}" style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end;padding:8px 0">
                    <div><label>Países (ou ALL)</label><input type="text" name="paises_acesso" value="{{ u.paises_acesso if u.paises_acesso else 'ALL' }}" class="inp inp-md"></div>
                    <div><label>Vendedores (ou ALL)</label><input type="text" name="vendedores_acesso" value="{{ u.vendedores_acesso if u.vendedores_acesso else 'ALL' }}" class="inp inp-md"></div>
                    <div style="display:flex;align-items:center;gap:6px;padding-bottom:4px">
                        <input type="checkbox" name="pode_exportar" value="1" {% if u.pode_exportar %}checked{% endif %}>
                        <label style="margin:0;color:#e2e8f0">Exportar CSV</label>
                    </div>
                    <div><label>Nova senha (opcional)</label><input type="text" name="new_password" class="inp inp-sm" placeholder="deixe vazio"></div>
                    <button type="submit" class="btn btn-primary btn-sm">Salvar</button>
                </form>
            </td>
        </tr>
        {% endif %}
        {% endfor %}
        </tbody>
    </table>
</div>
</body></html>"""

ADMIN_STATS_HTML = """<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Estatísticas — BYERESERVAME</title>""" + BASE_CSS + """</head><body>
<div class="navbar">
    <div class="brand">BYE<span>RESERVAME</span></div>
    <nav>
        <a href="{{ url_for('index') }}">Vendas</a>
        <a href="{{ url_for('admin_stats') }}" style="color:#f8fafc">Estatísticas</a>
        <a href="{{ url_for('admin_users') }}">Usuários</a>
        <a href="{{ url_for('admin_access_log') }}">Acessos</a>
        <a href="{{ url_for('admin_import') }}">Importar</a>
    </nav>
    <div class="user-info">{{ user.nome }} · <a href="{{ url_for('logout') }}" style="color:#f43f5e">Sair</a></div>
</div>
<div class="container">
    <div class="stats-bar">
        <div class="stat-card"><div class="num">{{ "{:,}".format(total_vendas).replace(",",".") }}</div><div class="label">Total Vendas</div></div>
        <div class="stat-card"><div class="num">{{ "{:,}".format(total_obs).replace(",",".") }}</div><div class="label">Observações</div></div>
        <div class="stat-card"><div class="num">{{ "{:,}".format(total_anexos).replace(",",".") }}</div><div class="label">Anexos</div></div>
    </div>

    <div class="detail-card">
        <h3>Vendas por Mês</h3>
        <table>
            <thead><tr><th>Ano</th><th>Mês</th><th>Vendas</th><th>Vendedores</th></tr></thead>
            <tbody>
            {% for m in by_month %}
            <tr><td>{{ m.ano }}</td><td>{{ m.mes }}</td><td>{{ m.qtd }}</td><td>{{ m.vendedores }}</td></tr>
            {% endfor %}
            </tbody>
        </table>
    </div>

    <div class="detail-card">
        <h3>Top 20 Vendedores</h3>
        <table>
            <thead><tr><th>#</th><th>Vendedor</th><th>Vendas</th></tr></thead>
            <tbody>
            {% for v in by_vendedor %}
            <tr><td>{{ loop.index }}</td><td>{{ v.vendedor }}</td><td>{{ v.qtd }}</td></tr>
            {% endfor %}
            </tbody>
        </table>
    </div>
</div>
</body></html>"""

ADMIN_IMPORT_HTML = """<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Importar — BYERESERVAME</title>""" + BASE_CSS + """</head><body>
<div class="navbar">
    <div class="brand">BYE<span>RESERVAME</span></div>
    <nav>
        <a href="{{ url_for('index') }}">Vendas</a>
        <a href="{{ url_for('admin_stats') }}">Estatísticas</a>
        <a href="{{ url_for('admin_users') }}">Usuários</a>
        <a href="{{ url_for('admin_access_log') }}">Acessos</a>
        <a href="{{ url_for('admin_import') }}" style="color:#f8fafc">Importar</a>
    </nav>
    <div class="user-info">{{ user.nome }} · <a href="{{ url_for('logout') }}" style="color:#f43f5e">Sair</a></div>
</div>
<div class="container">
    {% for cat, msg in get_flashed_messages(with_categories=true) %}
    <div class="flash flash-{{ cat }}">{{ msg }}</div>
    {% endfor %}

    <div class="detail-card">
        <h3>Status do Banco</h3>
        <div class="stat-card" style="display:inline-block"><div class="num">{{ "{:,}".format(total).replace(",",".") }}</div><div class="label">Vendas no banco</div></div>
    </div>

    <div class="detail-card">
        <h3>Importar Dados (Upload JSON)</h3>
        <p style="color:#94a3b8; margin-bottom:16px">Faça upload dos arquivos JSON exportados do sistema. O arquivo de vendas é obrigatório; o de detalhes (obs/anexos) é opcional.</p>
        <form method="POST" enctype="multipart/form-data">
            <input type="hidden" name="action" value="upload_json">
            <div style="margin-bottom:12px">
                <label style="color:#e2e8f0; display:block; margin-bottom:4px">vendas_XXXX.json (obrigatório)</label>
                <input type="file" name="vendas_file" accept=".json" required style="color:#e2e8f0">
            </div>
            <div style="margin-bottom:12px">
                <label style="color:#e2e8f0; display:block; margin-bottom:4px">details_XXXX.json (opcional — obs e anexos)</label>
                <input type="file" name="details_file" accept=".json" style="color:#e2e8f0">
            </div>
            <button type="submit" class="btn btn-primary" onclick="return confirm('Isso vai ADICIONAR os dados ao banco. Continuar?')">Importar</button>
        </form>
    </div>

    <div class="detail-card">
        <h3>Importar da pasta data/ (local)</h3>
        <p style="color:#94a3b8; margin-bottom:16px">Importa vendas_2026.json e details_2026.json da pasta data/ no servidor (se existirem).</p>
        <form method="POST">
            <input type="hidden" name="action" value="import_2026">
            <button type="submit" class="btn btn-primary" onclick="return confirm('Isso vai substituir todos os dados 2026. Continuar?')">Importar 2026 (local)</button>
        </form>
    </div>
</div>
</body></html>"""

ACCESS_LOG_HTML = """<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Log de Acessos — BYERESERVAME</title>""" + BASE_CSS + """</head><body>
<div class="navbar">
    <div class="brand">BYE<span>RESERVAME</span></div>
    <nav>
        <a href="{{ url_for('index') }}">Vendas</a>
        <a href="{{ url_for('admin_stats') }}">Estatísticas</a>
        <a href="{{ url_for('admin_users') }}">Usuários</a>
        <a href="{{ url_for('admin_access_log') }}" style="color:#f8fafc">Acessos</a>
        <a href="{{ url_for('admin_import') }}">Importar</a>
    </nav>
    <div class="user-info">{{ user.nome }} · <a href="{{ url_for('logout') }}" style="color:#f43f5e">Sair</a></div>
</div>
<div class="container">
    <div class="detail-card">
        <h3>Últimos 200 Acessos</h3>
        <table>
            <thead><tr><th>#</th><th>Usuário</th><th>Data/Hora</th><th>IP</th></tr></thead>
            <tbody>
            {% for log in logs %}
            <tr>
                <td>{{ log.id }}</td>
                <td>{{ log.username }}</td>
                <td>{{ log.login_at }}</td>
                <td>{{ log.ip or '—' }}</td>
            </tr>
            {% endfor %}
            {% if not logs %}
            <tr><td colspan="4" style="text-align:center;color:#64748b">Nenhum acesso registrado ainda.</td></tr>
            {% endif %}
            </tbody>
        </table>
    </div>
</div>
</body></html>"""

# ─── Init ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
else:
    init_db()

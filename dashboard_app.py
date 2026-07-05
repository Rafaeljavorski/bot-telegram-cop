import os
import sqlite3
from datetime import datetime
from flask import Flask, jsonify, render_template, request

DATABASE_PATH = os.getenv("DATABASE_PATH", "cop_bot.db")

app = Flask(__name__)


def db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def mins(dt):
    if not dt:
        return "-"
    try:
        d = datetime.fromisoformat(dt)
        return int((datetime.now() - d).total_seconds() // 60)
    except Exception:
        return "-"


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/dashboard")
def api_dashboard():
    with db() as conn:
        resumo = {
            "aguardando": conn.execute("SELECT COUNT(*) c FROM tickets WHERE status='aguardando'").fetchone()["c"],
            "em_atendimento": conn.execute("SELECT COUNT(*) c FROM tickets WHERE status='em_atendimento'").fetchone()["c"],
            "finalizados_hoje": conn.execute("""
                SELECT COUNT(*) c FROM tickets
                WHERE status='finalizado' AND date(closed_at)=date('now','localtime')
            """).fetchone()["c"],
            "total_hoje": conn.execute("""
                SELECT COUNT(*) c FROM tickets
                WHERE date(created_at)=date('now','localtime')
            """).fetchone()["c"],
        }

        aguardando = [dict(r) for r in conn.execute("""
            SELECT protocolo, user_name, categoria, contrato, created_at, fotos
            FROM tickets WHERE status='aguardando'
            ORDER BY id ASC
        """).fetchall()]

        atendimento = [dict(r) for r in conn.execute("""
            SELECT protocolo, user_name, categoria, contrato, atendente_nome, assumed_at, created_at, fotos
            FROM tickets WHERE status='em_atendimento'
            ORDER BY assumed_at ASC
        """).fetchall()]

        ranking = [dict(r) for r in conn.execute("""
            SELECT atendente_nome, COUNT(*) total
            FROM tickets
            WHERE status='finalizado' AND atendente_nome IS NOT NULL
              AND date(closed_at)=date('now','localtime')
            GROUP BY atendente_nome
            ORDER BY total DESC
            LIMIT 10
        """).fetchall()]

        por_fila = [dict(r) for r in conn.execute("""
            SELECT categoria, COUNT(*) total
            FROM tickets
            WHERE date(created_at)=date('now','localtime')
            GROUP BY categoria
            ORDER BY total DESC
        """).fetchall()]

        ultimos = [dict(r) for r in conn.execute("""
            SELECT protocolo, user_name, categoria, status, atendente_nome, created_at, closed_at
            FROM tickets
            ORDER BY id DESC
            LIMIT 30
        """).fetchall()]

    for item in aguardando:
        item["espera_min"] = mins(item["created_at"])
    for item in atendimento:
        item["tempo_atendimento_min"] = mins(item["assumed_at"])
        item["espera_total_min"] = mins(item["created_at"])

    resumo["maior_espera"] = max([i["espera_min"] for i in aguardando if isinstance(i["espera_min"], int)] or [0])

    return jsonify({
        "resumo": resumo,
        "aguardando": aguardando,
        "em_atendimento": atendimento,
        "ranking": ranking,
        "por_fila": por_fila,
        "ultimos": ultimos,
        "atualizado_em": datetime.now().strftime("%H:%M:%S")
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

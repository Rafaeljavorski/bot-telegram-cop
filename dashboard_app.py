"""
Dashboard web do COP CIP Telecom.

IMPORTANTE — isso roda como um processo SEPARADO do bot (bot_cop_telegram.py).
No Railway, isso significa um segundo serviço, apontando pra este mesmo
repositório, com start command `python dashboard_app.py` (o do bot continua
`python bot_cop_telegram.py`). Os dois processos se conectam ao MESMO banco
Postgres (mesma variável DATABASE_URL), então não precisa duplicar dado
nenhum — o dashboard só LÊ o que o bot já escreve.
"""
import os
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Configure a variável DATABASE_URL (a mesma usada pelo bot) no Railway.")

pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=5,
    kwargs={"row_factory": dict_row},
    open=False,
)
pool.open(wait=True, timeout=30)

app = Flask(__name__)


def minutos_desde(iso_str):
    """Minutos passados desde um timestamp ISO (string) até agora. None/erro -> None."""
    if not iso_str:
        return None
    try:
        d = datetime.fromisoformat(iso_str)
        return max(0, int((datetime.now() - d).total_seconds() // 60))
    except Exception:
        return None


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/dashboard")
@app.route("/api/dashboard/<int:dias>")
def api_dashboard(dias=1):
    dias = max(1, min(dias, 90))  # limite de segurança: no máx. 90 dias de uma vez

    # created_at é gravado como TEXTO em formato ISO ("AAAA-MM-DDTHH:MM:SS")
    # pelo bot. Comparação de texto nesse formato já é equivalente a
    # comparação cronológica, então calcula o corte em Python (mais fácil
    # de revisar/testar do que depender de sintaxe de INTERVAL do Postgres).
    corte = (datetime.now() - timedelta(days=dias - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat(timespec="seconds")

    with pool.connection() as conn:
        resumo = {
            "aguardando": conn.execute(
                "SELECT COUNT(*) c FROM tickets WHERE status='aguardando'"
            ).fetchone()["c"],
            "em_atendimento": conn.execute(
                "SELECT COUNT(*) c FROM tickets WHERE status='em_atendimento'"
            ).fetchone()["c"],
        }

        # Todos os chamados criados desde o corte (inclui os que já foram
        # finalizados ou ainda estão em aberto) — essa é a base de dados que
        # o front-end usa pra montar os gráficos e a tabela filtrável, tudo
        # do lado do cliente (sem precisar de mais idas e vindas ao servidor
        # a cada clique de filtro).
        tickets = conn.execute(
            """
            SELECT protocolo, user_name, categoria, subcategoria, contrato, status,
                   atendente_nome, created_at, assumed_at, closed_at
            FROM tickets
            WHERE created_at >= %s
            ORDER BY id DESC
            LIMIT 2000
            """,
            (corte,),
        ).fetchall()

    tickets_out = []
    esperas_aguardando = []
    for t in tickets:
        item = dict(t)
        if item["status"] == "aguardando":
            espera = minutos_desde(item["created_at"])
            item["espera_min"] = espera
            if espera is not None:
                esperas_aguardando.append(espera)
        else:
            item["espera_min"] = None
        tickets_out.append(item)

    resumo["maior_espera_min"] = max(esperas_aguardando) if esperas_aguardando else 0
    resumo["total_periodo"] = len(tickets_out)
    resumo["finalizados_periodo"] = sum(1 for t in tickets_out if t["status"] == "finalizado")

    return jsonify({
        "resumo": resumo,
        "tickets": tickets_out,
        "periodo_dias": dias,
        "atualizado_em": datetime.now().strftime("%H:%M:%S"),
    })


if __name__ == "__main__":
    try:
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
    finally:
        pool.close()
